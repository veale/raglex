"""Deterministic citation matchers — the cheap, high-confidence rungs of the §5b
resolution ladder. Each takes a raw citation string and returns a normalised
candidate id (and the method that produced it), or None.

Confirmation against the catalogue happens in the resolver; a matcher only
*proposes* a canonical id. Fuzzy/semantic (RapidFuzz/embeddings) and LLM rungs
land in later build steps; these structured matchers cover the common forms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candidate:
    value: str  # canonical id to confirm against the catalogue (stable_id / ECLI)
    method: str  # 'ecli' | 'celex' | 'uk_ncn' | 'legislation' | 'alias'


# ECLI: ECLI:<country>:<court>:<year>:<ordinal>  (technology-neutral work id, §1.1)
_ECLI_RE = re.compile(r"ECLI:[A-Z]{2}:[A-Z0-9]+:\d{4}:[A-Z0-9._-]+", re.IGNORECASE)

# CELEX (EU): 5-digit sector+year, 1-2 letter descriptor, 4-digit number, e.g.
# 32016R0679 (GDPR), 62019CJ0311 (Schrems II).
_CELEX_RE = re.compile(r"\b\d{5}[A-Z]{1,2}\d{4}\b", re.IGNORECASE)

# UK neutral citation: [2024] UKSC 12, [2024] EWHC 99 (Admin),
# [2026] UKFTT 904 (GRC). Optional bracketed division becomes a path segment.
_UK_NCN_RE = re.compile(
    r"\[(?P<year>\d{4})\]\s+(?P<court>[A-Z]{2,8})\s+(?P<num>\d+)"
    r"(?:\s+\((?P<sub>[A-Za-z]+)\))?"
)

# legislation.gov.uk identifier URI: .../id/ukpga/2000/36 or .../ukpga/2000/36
# Section fragments (.../section/14/1) are dropped — the candidate is the Act, so
# every section-level cite resolves to the same legislation node once harvested.
_LEG_URI_RE = re.compile(
    r"legislation\.gov\.uk/(?:id/)?(?P<path>[a-z]{2,6}/\d{4}/\d+)", re.IGNORECASE
)

# legislation.gov.uk's ASSIMILATED EU law (formerly "retained EU law"; renamed by the
# Retained EU Law (Revocation and Reform) Act 2023) — the UK-hosted, UK-amendable
# version of an EU instrument, at .../european/regulation/2016/0679. A UK judgment
# citing this URL means the *assimilated* version, NOT the EU original on CELLAR — so
# the candidate is the legislation.gov.uk path (fetched via uk-legislation, getting the
# amended UK text), kept distinct from the CELEX. Biggest "name-only" bucket.
_LEG_EU_RE = re.compile(
    r"legislation\.gov\.uk/(?:id/)?(?P<path>european/(?:regulation|directive|decision)/\d{4}/\d+)",
    re.IGNORECASE,
)
# legislation.gov.uk URI for a pre-1963 Act cited by regnal year, e.g.
# .../ukpga/Geo6/9-10/18 (9-10 Geo. 6 c. 18) — the year segment isn't a calendar year.
_LEG_REGNAL_RE = re.compile(
    r"legislation\.gov\.uk/(?:id/)?(?P<path>[a-z]{2,6}/[A-Za-z][A-Za-z0-9]*/[\d-]+/\d+)",
    re.IGNORECASE,
)

# UK Find Case Law document URI — our own stable_id form, so an intra-corpus case
# citation resolves directly: .../ewca/civ/2015/454 or .../d-{uuid}.
_CASELAW_URI_RE = re.compile(
    r"caselaw\.nationalarchives\.gov\.uk/(?P<path>(?:d-[0-9a-f-]+|[a-z]+(?:/[a-z]+)*/\d{4}/\d+))",
    re.IGNORECASE,
)


def match_ecli(raw: str) -> Candidate | None:
    m = _ECLI_RE.search(raw)
    if not m:
        return None
    return Candidate(value=m.group(0).upper(), method="ecli")


def match_celex(raw: str) -> Candidate | None:
    m = _CELEX_RE.search(raw)
    if not m:
        return None
    return Candidate(value=m.group(0).upper(), method="celex")


def match_uk_ncn(raw: str) -> Candidate | None:
    """Map a UK neutral citation to its Find Case Law document URI form
    (``court[/sub]/year/number``), mirroring how we mint stable_ids for UK docs."""
    m = _UK_NCN_RE.search(raw)
    if not m:
        return None
    court = m.group("court").lower()
    parts = [court]
    if m.group("sub"):
        parts.append(m.group("sub").lower())
    parts.extend([m.group("year"), m.group("num")])
    return Candidate(value="/".join(parts), method="uk_ncn")


def match_legislation_uri(raw: str) -> Candidate | None:
    m = _LEG_URI_RE.search(raw)
    if not m:
        return None
    return Candidate(value=m.group("path").lower(), method="legislation")


def match_legislation_eu_uri(raw: str) -> Candidate | None:
    """legislation.gov.uk ASSIMILATED EU law → its legislation.gov.uk path (so it's
    fetched via uk-legislation as the UK-amended version, kept distinct from the EU
    original it's an assimilated version of)."""
    m = _LEG_EU_RE.search(raw)
    if not m:
        return None
    return Candidate(value=m.group("path").lower(), method="assimilated_eu")


def assimilated_celex(path: str) -> str | None:
    """The CELEX of the EU original an assimilated-law path is a version of —
    ``european/regulation/2016/0679`` → ``32016R0679`` (for the assimilated_version_of edge).
    Handles both the ``/european/{kind}/…`` form and legislation.gov.uk's type-code form
    (``eur/2008/1272`` → ``32008R1272``, ``eudr/2000/60`` → ``32000L0060``)."""
    p = path.lower()
    m = re.match(r"european/(regulation|directive|decision)/(\d{4})/(\d+)$", p)
    if m:
        desc = {"regulation": "R", "directive": "L", "decision": "D"}[m.group(1)]
        return f"3{m.group(2)}{desc}{int(m.group(3)):04d}"
    m = re.match(r"(eur|eudr|eudn|eudc)/(\d{4})/(\d+)$", p)
    if m:
        desc = {"eur": "R", "eudr": "L", "eudn": "D", "eudc": "D"}[m.group(1)]
        return f"3{m.group(2)}{desc}{int(m.group(3)):04d}"
    return None


def match_legislation_regnal(raw: str) -> Candidate | None:
    """A pre-1963 Act cited by regnal year (ukpga/Geo6/9-10/18). Keep the original
    case — legislation.gov.uk's regnal segment ("Geo6") is case-sensitive in the URI."""
    m = _LEG_REGNAL_RE.search(raw)
    if not m:
        return None
    return Candidate(value=m.group("path"), method="legislation_regnal")


def match_caselaw_uri(raw: str) -> Candidate | None:
    m = _CASELAW_URI_RE.search(raw)
    if not m:
        return None
    return Candidate(value=m.group("path").lower(), method="caselaw_uri")


# Ladder order: most specific / highest-confidence first (§5b). The caselaw-URI
# matcher runs before the NCN matcher so an explicit document URI wins.
MATCHERS = (
    match_ecli,
    match_celex,
    match_caselaw_uri,
    match_legislation_eu_uri,   # /european/… before the generic legislation URI
    match_legislation_uri,
    match_legislation_regnal,
    match_uk_ncn,
)


def first_candidate(raw: str) -> Candidate | None:
    for matcher in MATCHERS:
        cand = matcher(raw)
        if cand is not None:
            return cand
    return None


def extract_citation_strings(text: str) -> list[str]:
    """Find every ECLI / CELEX literal in a free-text blob (e.g. a Zotero abstract
    or an imported article), de-duplicated in order. Each becomes a dangling
    ``mentions`` edge that the resolver (§5b) links once the target is in corpus."""
    seen: dict[str, None] = {}
    for m in _ECLI_RE.finditer(text):
        seen.setdefault(m.group(0).upper(), None)
    for m in _CELEX_RE.finditer(text):
        seen.setdefault(m.group(0).upper(), None)
    return list(seen)
