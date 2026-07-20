"""The citation snowball (§5, §5a) — turn what the corpus *cites* into what to
*harvest* next.

Every extraction pass writes hanging edges to references the corpus doesn't yet
hold. Resolution links the ones whose targets have been harvested; the rest sit
pending. Read those pending candidates back through the *form* that produced them
and a picture of the frontier emerges:

- **CELEX** ``32016R0679`` → an EU regulation; harvestable via ``eu-legislation``.
- **CJEU CELEX** ``62018CJ0311`` → a CJEU judgment; harvestable via ``eu-cellar``.
- **ECLI** ``ECLI:NL:HR:2021:1234`` → a national judgment; the country code says
  *which* jurisdiction (NL → ``nl-rechtspraak``), even before an adapter exists.
- **Neutral citation** ``ewhc/2024/1`` → a common-law judgment; the court token
  (``EWHC``) is looked up in the court registry (``citations.courts``).

So a candidate is classified into ``(form, jurisdiction, adapter)`` *without
knowing the specific case* — which is exactly the snowball: detect a citation's
shape, infer where it lives, and either queue it against an existing adapter or
flag it as "a body we cite often but can't harvest yet — build an adapter". The
result ranks by how often the corpus reaches for each frontier so the highest-
value harvest (or new-adapter) work floats up.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from ..storage.catalogue import Catalogue
from .courts import classify

# CELEX sector/descriptor → human form + the adapter that fetches it. The number
# part is variable-width (treaty articles like 12008E267 = Art 267 TFEU, Charter
# 12007P008) and may carry a "(01)" suffix — so match 1–4 digits + optional suffix,
# not a fixed 4-digit block.
_CELEX_RE = re.compile(r"^(?P<sector>[1-9])\d{4}(?P<desc>[A-Z]{1,2})\d{1,4}(?:\(\d+\))?$", re.IGNORECASE)
_CELEX_DESC = {"R": "EU regulation", "L": "EU directive", "D": "EU decision"}
_ECLI_RE = re.compile(r"^ECLI:(?P<country>[A-Z]{2}):(?P<court>[A-Z0-9]+):", re.IGNORECASE)
_NEUTRAL_RE = re.compile(r"^(?P<court>[a-z]+)(?:/[a-z]+)?/(?:19|20)\d{2}/\d+$")
# ECLI country → the adapter that can fetch it today (extend as adapters land). "CE"
# (Council of Europe) is the ECtHR — ECLI:CE:ECHR:… → the HUDOC adapter.
_ECLI_ADAPTER = {"EU": "eu-cellar", "NL": "nl-rechtspraak", "GB": "uk-caselaw", "CE": "echr"}
# A bare ECHR application number (4451/70, 36022/97) — the resolvable key for an ECtHR
# case (the HUDOC adapter looks it up). Distinct from a CJEU number, which has a C-/T- prefix.
ECHR_APPNO_RE = re.compile(r"^\d{1,5}/\d{2}$")  # app-number year is always 2 digits
_ECHR_APPNO_RE = ECHR_APPNO_RE  # back-compat alias
# UK legislation slug prefixes (ukpga/1998/42, nisi/1981/1675, wsi/2016/413) —
# distinct from neutral citations. legislation.gov.uk hosts ALL UK jurisdictions
# (England/Wales/Scotland/NI), so every one of these is fetchable via uk-legislation.
UK_LEG_TYPES = {
    "ukpga", "ukla", "uksi", "ukcm", "ukmo", "uksro", "gbla", "gbppp",  # UK-wide
    "asp", "ssi", "asc", "aosp",                                        # Scotland
    "anaw", "mwa", "wsi", "asc",                                        # Wales
    "nia", "apni", "nisi", "nisr", "nisro", "mnia", "aip", "apgb", "aep",  # NI / historic
}
_UK_LEG_TYPES = UK_LEG_TYPES  # back-compat alias

# Split the UK legislation types into primary (Acts/Measures), secondary (statutory
# instruments / rules / orders), and assimilated (retained direct EU law) — so the
# worklist can be filtered and harvested one category at a time.
PRIMARY_LEG_TYPES = {
    "ukpga", "ukla", "asp", "anaw", "asc", "nia", "mwa", "ukcm", "apni",
    "mnia", "aosp", "aep", "apgb", "aip", "gbla",
}
SECONDARY_LEG_TYPES = {"uksi", "ssi", "wsi", "nisr", "nisro", "ukmo", "uksro", "nisi", "gbppp"}
ASSIMILATED_LEG_TYPES = {"eur", "eudr", "eudn", "eudc", "eufr", "european"}


def uk_leg_category(candidate: str | None) -> str | None:
    """A UK legislation candidate → "primary" | "secondary" | "assimilated" (else None)."""
    if not candidate or "/" not in candidate:
        return None
    head = candidate.split("/", 1)[0].lower()
    if head in PRIMARY_LEG_TYPES:
        return "primary"
    if head in SECONDARY_LEG_TYPES:
        return "secondary"
    if head in ASSIMILATED_LEG_TYPES:
        return "assimilated"
    return None

# legislation.gov.uk's *type-code* identifiers for assimilated (retained) EU law —
# the older sibling of the /european/{regulation|directive|decision}/… form. These ARE
# fetchable via uk-legislation (e.g. /eur/2008/1272/data.akn), so they must route there,
# not be mistaken for a neutral-citation court token ("EUDR", "EUR").
EU_ASSIMILATED_TYPES = {"eur", "eudr", "eudn", "eudc", "eufr"}


@dataclass(slots=True)
class Frontier:
    """One harvestable frontier the corpus keeps citing."""

    form: str  # "EU regulation" | "CJEU judgment" | "neutral citation (EWHC)" | …
    jurisdiction: str | None  # EU, NL, GB, CA, …
    adapter: str | None  # the source that can fetch it today, or None → build one
    candidates: int  # distinct references of this form not yet resolved
    occurrences: int  # total mentions across the corpus
    documents: int  # distinct citing documents
    sample: str  # an example candidate id (for the operator/agent)
    harvestable: bool  # True if an adapter exists; False → snowball needs an adapter


def _classify(candidate: str, kind: str) -> tuple[str, str | None, str | None]:
    """(form, jurisdiction, adapter) for a candidate id, from its shape alone."""
    # An ECtHR case cited by name (EHRR grammar) — the candidate is "echr:<case name>",
    # resolved via a HUDOC docname search by the echr adapter (inferred, fuzzy).
    if kind == "echr_case" or candidate.lower().startswith("echr:"):
        return "ECHR case (by name)", "CoE", "echr"
    m = _CELEX_RE.match(candidate)
    if m:
        sector = m.group("sector")
        if sector == "6":
            return "CJEU judgment", "EU", "eu-cellar"
        if sector == "1":  # treaties / Charter / primary law (TFEU, TEU, CFR)
            return "EU treaty / primary law", "EU", "eu-legislation"
        return _CELEX_DESC.get(m.group("desc").upper()[0], "EU instrument"), "EU", "eu-legislation"
    m = _ECLI_RE.match(candidate)
    if m:
        country = m.group("country").upper()
        return f"ECLI judgment ({country})", country, _ECLI_ADAPTER.get(country)
    if _ECHR_APPNO_RE.match(candidate):
        return "ECHR application no.", "CoE", "echr"
    if candidate.lower() == "echr/convention":
        return "ECHR (Convention)", "CoE", None  # the treaty node; added in-corpus, not crawled
    # Assimilated EU law (legislation.gov.uk /european/…) — the UK-hosted version,
    # fetched via uk-legislation (distinct from the EU original it assimilates).
    head = candidate.split("/", 1)[0].lower() if "/" in candidate else ""
    if candidate.lower().startswith("european/") or head in EU_ASSIMILATED_TYPES:
        return "Assimilated EU law (UK)", "GB", "uk-legislation"
    # UK legislation ids share the slug shape (ukpga/1998/42) — classify them
    # *before* the neutral-citation regex so they aren't mistaken for a court.
    if "/" in candidate and candidate.split("/")[0] in _UK_LEG_TYPES:
        return "UK legislation", "GB", "uk-legislation"
    # US reporter citations: recognised and clustered, but the corpus
    # holds no US case law and has no adapter — so it reads as a US case in the
    # frontier and stays OUT of the routable harvest worklist.
    if head == "us":
        return "US case (reporter)", "US", None
    m = _NEUTRAL_RE.match(candidate)
    if m:
        court = m.group("court").upper()
        known = classify(court)
        if known and not known.generic:
            return (f"neutral citation ({court})", known.jurisdiction, known.adapter)
        if known:
            # The tribunal isn't registered, but the medium-neutral-citation convention
            # puts the ISO country code first, so the citation still has a country. It
            # stays in the snowball (no adapter) while reading as e.g. Kenyan case law
            # rather than as an unplaceable unknown.
            return (f"neutral citation ({court})", known.jurisdiction, None)
        return (f"neutral citation ({court})", None, None)  # unknown court → snowball
    return (kind or "citation"), None, None


def snowball(catalogue: Catalogue, *, limit: int = 50, only_unresolved: bool = True,
             only_unharvestable: bool = False) -> list[dict]:
    """Rank the corpus's citation frontier. ``only_unresolved`` (default) keeps just
    the candidates that aren't already nodes — the actual harvest worklist;
    ``only_unharvestable`` narrows further to forms with no adapter — the
    "build-an-adapter" list."""
    buckets: dict[tuple[str, str | None, str | None], Frontier] = {}
    rows = catalogue.candidate_frequencies()
    # Which of these candidates are already nodes? Build the set of held keys once (two
    # cheap scans) and test membership — 165k point lookups / batched OR-queries were the
    # whole cost of this endpoint (a minute-plus over the production graph).
    held: set = catalogue.held_key_set() if only_unresolved else set()
    for row in rows:
        candidate = row["candidate_id"]
        if only_unresolved and (candidate in held or candidate.casefold() in held):
            continue  # already a node — not part of the frontier
        form, juris, adapter = _classify(candidate, row["entity_kind"])
        key = (form, juris, adapter)
        f = buckets.get(key)
        if f is None:
            f = buckets[key] = Frontier(
                form=form, jurisdiction=juris, adapter=adapter, candidates=0,
                occurrences=0, documents=0, sample=candidate, harvestable=adapter is not None,
            )
        f.candidates += 1
        f.occurrences += row["occurrences"]
        f.documents += row["documents"]
    out = sorted(buckets.values(), key=lambda f: (f.occurrences, f.candidates), reverse=True)
    if only_unharvestable:
        out = [f for f in out if not f.harvestable]
    return [asdict(f) for f in out[:limit]]
