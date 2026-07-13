"""Corpus taxonomy — map any held document OR any pending citation to a single
``(category, sub-type)`` pair, so the dashboard's Corpus Map can tally held-vs-pending
the same way for both. One source of truth for "what kind of legal thing is this".

The category key is the *adapter* that fetches the thing (``uk-legislation``, ``eu-cellar``,
…), so it lines up with the harvest endpoints (``harvest_all_references(adapter=…)``). Sub-type
splits each category the way a lawyer would: SIs vs Acts vs assimilated (and by UK nation),
CJEU vs General Court vs AG opinions, by UK court/tribunal, ECHR cases vs the Convention.

Reuses the slug/CELEX classification already in :mod:`.snowball` and the court registry in
:mod:`.courts`; adds only the sub-type split on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .courts import KNOWN_COURTS
from .snowball import (
    ASSIMILATED_LEG_TYPES,
    PRIMARY_LEG_TYPES,
    SECONDARY_LEG_TYPES,
    _classify,
    _CELEX_RE,
)

# Category keys == the fetching adapter, so a row's "Harvest" action maps straight to
# harvest_all_references(adapter=…). "other" collects anything unrouted.
CATEGORY_LABELS: dict[str, str] = {
    "uk-caselaw": "UK case-law",
    "uk-legislation": "UK legislation",
    "ie-caselaw": "Irish case-law",
    "ie-legislation": "Irish legislation",
    "eu-cellar": "EU case-law",
    "eu-legislation": "EU legislation",
    "echr": "ECHR",
    "ca-caselaw": "Canadian case-law",
    "au-caselaw": "Australian case-law",
    "nz-caselaw": "New Zealand case-law",
    "in-caselaw": "Indian case-law",
    "other": "Other / unrouted",
}
CATEGORY_ORDER = ["uk-caselaw", "uk-legislation", "ie-caselaw", "ie-legislation",
                  "eu-cellar", "eu-legislation", "echr",
                  "ca-caselaw", "au-caselaw", "nz-caselaw", "in-caselaw", "other"]

# Neutral-citation jurisdictions with no adapter (cases arrive by upload, if at all):
# the KNOWN_COURTS jurisdiction → the Corpus Map bucket. A "[2020] NZSC 12" pending
# citation should read as New Zealand case-law, not "Other / unrouted" — the corpus
# understanding these places precedes any import route existing.
JURISDICTION_CATEGORY: dict[str, str] = {
    "IE": "ie-caselaw", "CA": "ca-caselaw", "AU": "au-caselaw",
    "NZ": "nz-caselaw", "IN": "in-caselaw",
}

# Which UK nation a legislation type-code belongs to (for the SI/Act-by-country split).
UK_LEG_COUNTRY: dict[str, str] = {
    # UK-wide
    "ukpga": "UK-wide", "uksi": "UK-wide", "ukla": "UK-wide", "ukcm": "UK-wide",
    "ukmo": "UK-wide", "uksro": "UK-wide", "gbla": "UK-wide", "gbppp": "UK-wide",
    "apgb": "UK-wide", "aep": "UK-wide", "aip": "UK-wide",
    # Scotland
    "asp": "Scotland", "ssi": "Scotland", "aosp": "Scotland",
    # Wales
    "anaw": "Wales", "asc": "Wales", "mwa": "Wales", "wsi": "Wales",
    # Northern Ireland
    "nia": "N. Ireland", "apni": "N. Ireland", "nisi": "N. Ireland", "nisr": "N. Ireland",
    "nisro": "N. Ireland", "mnia": "N. Ireland",
}
_LEG_KIND_LABEL = {"primary": "Primary", "secondary": "Secondary", "assimilated": "Assimilated"}
_CELEX_LEG_DESC = {"R": ("reg", "Regulation"), "L": ("dir", "Directive"),
                   "D": ("dec", "Decision")}

# Irish legislation sub-types (irishstatutebook.ie eli shapes: eli/YYYY/act/N,
# eli/YYYY/si/N, plus the Constitution). The category exists ahead of any harvest
# adapter — nothing populates it yet; it's the bucket Irish acts will land in.
IE_LEG_TYPES: dict[str, tuple[str, str]] = {
    "act": ("act", "Act of the Oireachtas"),
    "si": ("si", "Statutory Instrument (IE)"),
    "const": ("const", "Constitution of Ireland"),
}


@dataclass(frozen=True, slots=True)
class Tax:
    """A document/candidate's place in the corpus. ``filter`` is the query that deep-links
    the Corpus browser to exactly this set of *held* items (best-effort per category)."""

    category: str          # adapter key, e.g. "uk-legislation"
    category_label: str
    subtype: str           # stable sub-type key, e.g. "secondary:UK-wide"
    subtype_label: str     # human label, e.g. "Secondary · UK-wide"
    filter: dict           # {source, doc_type?, court?, id_prefix?} for list_documents


def _leg_subtype(slug_prefix: str) -> tuple[str, str]:
    """A UK legislation type-code → (sub_key, sub_label) combining kind + nation."""
    head = slug_prefix.lower()
    if head in PRIMARY_LEG_TYPES:
        kind = "primary"
    elif head in SECONDARY_LEG_TYPES:
        kind = "secondary"
    elif head in ASSIMILATED_LEG_TYPES or head == "european":
        return "assimilated", "Assimilated EU law"
    else:
        return "other", "Other"
    country = UK_LEG_COUNTRY.get(head, "UK-wide")
    return f"{kind}:{country}", f"{_LEG_KIND_LABEL[kind]} · {country}"


def _eu_case_subtype(doc_type: str | None, court: str | None, celex: str | None) -> tuple[str, str]:
    """EU case-law sub-type from the stored court/doc_type, falling back to the CELEX
    descriptor (…CJ…/…TJ…/…CC…/…CO…) when court isn't stored (pending candidates)."""
    c = (court or "").lower()
    if doc_type == "opinion" or "advocate" in c:
        return "ag", "AG Opinion"
    if "general court" in c:
        return "gc", "General Court"
    if "court of justice" in c:
        return "cj", "CJEU judgment"
    # pending candidate: read the CELEX descriptor (6 2018 CJ 0311)
    m = re.match(r"^6\d{4}([A-Z]{2})", (celex or "").upper())
    if m:
        return {"CJ": ("cj", "CJEU judgment"), "TJ": ("gc", "General Court"),
                "CC": ("ag", "AG Opinion"), "CO": ("order", "Order"),
                "CO2": ("order", "Order")}.get(m.group(1), ("cj", "CJEU judgment"))
    return "cj", "CJEU judgment"


def _eu_leg_subtype(celex: str) -> tuple[str, str]:
    m = _CELEX_RE.match(celex or "")
    if m:
        if m.group("sector") == "1":
            return "treaty", "Treaty / primary law"
        key, label = _CELEX_LEG_DESC.get(m.group("desc").upper()[0], ("other", "Other instrument"))
        return key, label
    return "other", "Other instrument"


# HUDOC formation (doctypebranch) → (sub_key, sub_label) for the ECHR held split.
_ECHR_FORMATION = {
    "GRANDCHAMBER": ("gc", "Grand Chamber"),
    "CHAMBER": ("chamber", "Chamber"),
    "COMMITTEE": ("committee", "Committee"),
    "ADMISSIBILITY": ("decision", "Admissibility decision"),
    "DECCOMMISSION": ("commission", "Commission decision"),
    "REPORTS": ("report", "Commission report"),
}


def echr_formation(branch: str | None) -> tuple[str, str]:
    """A HUDOC ``doctypebranch`` value → (sub_key, sub_label). Unknown/blank → catch-all."""
    return _ECHR_FORMATION.get((branch or "").strip().upper(), ("other", "Other / unspecified"))


def classify_document(*, source: str, doc_type: str | None = None, court: str | None = None,
                      stable_id: str = "") -> Tax:
    """Place a HELD document (from its stored columns)."""
    # the held path passes the bare slug-head ("uksi"); the full-id path ("uksi/2016/413")
    # collapses to the same — either way the part before the first '/'.
    prefix = stable_id.split("/", 1)[0].lower()
    if source == "uk-legislation":
        sub, label = _leg_subtype(prefix)
        return Tax("uk-legislation", CATEGORY_LABELS["uk-legislation"], sub, label,
                   {"source": "uk-legislation", "id_prefix": prefix})
    # The House of Lords scraper (uk-hol) shares the UK case-law taxonomy: its ukhl/YYYY/N
    # (and pre-2001 hol/ surrogate) cases belong under the same "House of Lords" sub-type as
    # the pending "[YYYY] UKHL N" citations, so held + pending line up in one row.
    if source in ("uk-caselaw", "uk-hol"):
        tok = (court or prefix or "").upper()
        known = KNOWN_COURTS.get(tok)
        # opaque Find Case Law identifiers (tna.5mz…, d-uuid) aren't a court token — they'd
        # otherwise each show as their own junk sub-type row; bucket them as uncategorised.
        if not known and (tok.startswith(("TNA.", "D-")) or not tok.replace("/", "").isalpha()
                          or len(tok) > 8):
            return Tax("uk-caselaw", CATEGORY_LABELS["uk-caselaw"], "other",
                       "Other / uncategorised", {"source": source})
        # a court sub-type filters by court token (not source), so the list shows the court's
        # cases whether they came from Find Case Law or the House of Lords scrape.
        return Tax("uk-caselaw", CATEGORY_LABELS["uk-caselaw"], tok.lower() or "other",
                   known.name if known else (tok or "Other court"),
                   {"court": (court or prefix or "")})
    if source in ("ie-caselaw", "ca-caselaw", "au-caselaw", "nz-caselaw", "in-caselaw"):
        tok = (court or prefix or "").upper()
        known = KNOWN_COURTS.get(tok)
        return Tax(source, CATEGORY_LABELS[source], tok.lower() or "other",
                   known.name if known else (tok or "Other court"),
                   {"court": (court or prefix or "")})
    if source == "ie-legislation":
        sub, label = IE_LEG_TYPES.get(prefix, ("other", "Other"))
        return Tax("ie-legislation", CATEGORY_LABELS["ie-legislation"], sub, label,
                   {"source": "ie-legislation", "id_prefix": prefix})
    if source == "eu-cellar":
        sub, label = _eu_case_subtype(doc_type, court, stable_id)
        filt = {"source": "eu-cellar"}
        if sub == "ag":
            filt["doc_type"] = "opinion"
        elif court:
            filt["court"] = court
        return Tax("eu-cellar", CATEGORY_LABELS["eu-cellar"], sub, label, filt)
    if source == "eu-legislation":
        sub, label = _eu_leg_subtype(stable_id)
        return Tax("eu-legislation", CATEGORY_LABELS["eu-legislation"], sub, label,
                   {"source": "eu-legislation"})
    if source == "echr":
        # "echr" arrives when grouped by slug-prefix (echr/convention → "echr"); a held
        # ECtHR case is an ECLI:CE:… id with no slash, so this only catches the Convention.
        if stable_id in ("echr/convention", "echr"):
            return Tax("echr", CATEGORY_LABELS["echr"], "convention", "Convention (treaty)",
                       {"source": "echr", "id_prefix": "echr"})
        return Tax("echr", CATEGORY_LABELS["echr"], "case", "ECHR case",
                   {"source": "echr"})
    return Tax("other", CATEGORY_LABELS["other"], source or "other", source or "Other",
               {"source": source} if source else {})


def classify_candidate(candidate: str, kind: str = "") -> Tax:
    """Place a PENDING citation (a not-yet-held reference) by its shape alone, so pending
    tallies line up with held tallies in the same category/sub-type buckets."""
    _form, _juris, adapter = _classify(candidate, kind)
    cand = candidate or ""
    if adapter == "uk-legislation":
        prefix = cand.split("/", 1)[0].lower() if "/" in cand else cand.lower()
        sub, label = _leg_subtype(prefix)
        return Tax("uk-legislation", CATEGORY_LABELS["uk-legislation"], sub, label,
                   {"source": "uk-legislation", "id_prefix": prefix})
    if adapter == "uk-caselaw":
        tok = cand.split("/", 1)[0].upper() if "/" in cand else ""
        known = KNOWN_COURTS.get(tok)
        return Tax("uk-caselaw", CATEGORY_LABELS["uk-caselaw"], tok.lower() or "other",
                   known.name if known else (tok or "Other court"), {"source": "uk-caselaw"})
    if adapter == "eu-cellar":
        sub, label = _eu_case_subtype(None, None, cand)
        return Tax("eu-cellar", CATEGORY_LABELS["eu-cellar"], sub, label, {"source": "eu-cellar"})
    if adapter == "eu-legislation":
        sub, label = _eu_leg_subtype(cand)
        return Tax("eu-legislation", CATEGORY_LABELS["eu-legislation"], sub, label,
                   {"source": "eu-legislation"})
    if adapter == "echr":
        if cand.lower() == "echr/convention":
            return Tax("echr", CATEGORY_LABELS["echr"], "convention", "Convention (treaty)", {})
        return Tax("echr", CATEGORY_LABELS["echr"], "case", "ECHR case", {"source": "echr"})
    # Adapter-less neutral-citation courts: a recognised court token still buckets the
    # candidate by its jurisdiction — Irish/Commonwealth citations are first-class
    # pending case-law (upload/link resolves them), and NI/Scottish courts belong
    # under UK case-law with their court sub-type, never "other".
    head = cand.split("/", 1)[0].lower() if "/" in cand else ""
    known = KNOWN_COURTS.get(head.upper()) if head else None
    if known:
        catkey = JURISDICTION_CATEGORY.get(known.jurisdiction)
        if catkey:
            return Tax(catkey, CATEGORY_LABELS[catkey], head, known.name,
                       {"source": catkey})
        if known.jurisdiction == "GB":
            return Tax("uk-caselaw", CATEGORY_LABELS["uk-caselaw"], head, known.name,
                       {"court": head})
    return Tax("other", CATEGORY_LABELS["other"], "other", _form or "Other", {})
