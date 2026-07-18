"""Commonwealth citation forms that break the generic grammar.

Most common-law citations fit one of two shapes the core grammars already handle:
``[YEAR] COURT SEQ`` (neutral) and ``[YEAR] VOL REPORTER PAGE`` / ``(YEAR) VOL REPORTER
PAGE`` (reported). This module covers the jurisdictions whose dominant citation form
fits *neither*, and which would otherwise be silently mis-parsed rather than merely
unrecognised — the worse failure, because a wrong parse mints a wrong edge.

Each form here is registered as its own grammar because the extractor resolves overlaps
by **longest match**, so a specific rule reliably beats the generic one it sits inside:
``AIR 1973 SC 1461`` beats the bracketless-neutral read of ``1973 SC 1461`` (which would
otherwise treat the Indian reporter's embedded court token as a Scottish one).

What gets a resolvable candidate, and what deliberately doesn't:

* **CanLII ids** and **Indian neutral citations** are stable, addressable identifiers, so
  they mint a candidate slug and can resolve if an adapter ever lands.
* **Reported citations** (SA, NWLR, AIR, MLJ…) name a page in a printed series with no
  fetchable id, so they stay candidate-less — recognised and countable, never a fake id.
* **Hong Kong registry case numbers** (``FACV 1/2018``) are neither: they identify a case
  in a court's own file system. They are recognised precisely so they are *not* read as
  neutral citations, and stay candidate-less.

None of these jurisdictions has a fetch adapter today. That is the point: recognising the
form is what lets the citation be counted, classified and ranked in the snowball as
evidence of what to build next.
"""

from __future__ import annotations

import re

from .grammars import DROP, Grammar, Normalised, register

# Hong Kong registry case-number prefixes → the court they belong to. Pre-2018 HK cases
# are cited this way (neutral citation only arrived with Practice Direction 5.5), and the
# "/YYYY" suffix is the branch signal: a trailing /year means registry number, not neutral.
HK_REGISTRY_PREFIXES: dict[str, str] = {
    "FACV": "HKCFA", "FACC": "HKCFA", "FAMV": "HKCFA",     # Court of Final Appeal
    "CACV": "HKCA", "CACC": "HKCA", "CAAR": "HKCA",         # Court of Appeal
    "HCA": "HKCFI", "HCAL": "HKCFI", "HCMP": "HKCFI",       # Court of First Instance
    "HCCT": "HKCFI", "HCCW": "HKCFI", "HCB": "HKCFI",
    "DCCJ": "HKDC", "DCEO": "HKDC", "DCCC": "HKDC",         # District Court
}

# Indian High Court codes used in the 2023 colon-delimited neutral citation.
_INDIAN_COURT = (r"INSC|DHC|BHC|MHC|CHC|KAHC|KHC|AHC|GUJHC|PHHC|TSHC|"
                 r"[A-Z]{2,6}HC")

_YEAR = r"(?:19|20)\d{2}"


def _candidateless(_m: "re.Match[str]") -> Normalised:
    """Recognised, but no fetchable id — the established idiom for report citations
    (see citations.reporters). Keeps the reference visible and rankable without
    inventing an identifier that could never resolve."""
    return None, None, "case"


# -- India ------------------------------------------------------------------
# The 2023 neutral citation is colon-delimited and year-first — unlike any other system
# in the corpus: "2023:DHC:1234", and the Supreme Court's "2023 INSC 445" (which the
# bracketless grammar already reads). Rollout is recent and uneven across High Courts.
def _indian_neutral(m: "re.Match[str]") -> Normalised:
    return f"{m.group('court').lower()}/{m.group('year')}/{int(m.group('num'))}", None, "case"


register(Grammar(
    "in_neutral_colon", "case",
    re.compile(rf"\b(?P<year>{_YEAR}):(?P<court>{_INDIAN_COURT}):(?P<num>\d{{1,6}})\b"),
    _indian_neutral,
))

# AIR is the outlier of outliers: abbreviation FIRST, then year, then an embedded court
# token, then the page — "AIR 1973 SC 1461". Without this rule the trailing "1973 SC 1461"
# reads as a bracketless neutral citation whose court is Session Cases (Scotland).
register(Grammar(
    "in_air_report", "case",
    re.compile(rf"\bA\.?\s?I\.?\s?R\.?\s+(?P<year>{_YEAR})\s+"
               r"(?P<court>SC|SCC|PC|FC|[A-Z][a-z]{1,4})\s+(?P<page>\d{1,5})\b"),
    _candidateless,
))

# -- Canada -----------------------------------------------------------------
# Where a court has no official neutral citation the McGill Guide puts the CanLII id in
# the same slot, with the real court in trailing parentheses: "1998 CanLII 5115 (ONCA)".
# CanLII ids are stable and addressable, so this one does mint a candidate.
def _canlii(m: "re.Match[str]") -> Normalised:
    return f"canlii/{m.group('year')}/{int(m.group('num'))}", None, "case"


register(Grammar(
    "ca_canlii", "case",
    re.compile(rf"\b(?P<year>{_YEAR})\s+CanLII\s+(?P<num>\d{{1,7}})"
               r"(?:\s*\((?P<court>[A-Z]{2,8})\))?", re.IGNORECASE),
    _canlii,
))

# -- South Africa -----------------------------------------------------------
# "2004 (1) SA 406 (CC)" — year in the open, part number in round brackets, then the
# series, the page, and the COURT in trailing round brackets. The bare token "SA" is
# heavily overloaded (South Australia, "State … Australia"), so it is read as the South
# African Law Reports ONLY inside this full shape, which the collision table demands.
register(Grammar(
    "za_law_report", "case",
    re.compile(rf"\b(?P<year>{_YEAR})\s+\((?P<part>\d{{1,2}})\)\s+"
               r"(?P<series>SA|BCLR|SACR)\s+(?P<page>\d{1,4})"
               r"(?:\s*\((?P<court>[A-Z]{1,8})\))?"),
    _candidateless,
))

# "[2019] 2 All SA 1 (SCA)" — the All South African Law Reports, square-bracket form.
register(Grammar(
    "za_all_sa_report", "case",
    re.compile(rf"\[(?P<year>{_YEAR})\]\s+(?:(?P<vol>\d{{1,2}})\s+)?All\s+SA\s+"
               r"(?P<page>\d{1,4})(?:\s*\((?P<court>[A-Z]{1,8})\))?"),
    _candidateless,
))

# -- Nigeria ----------------------------------------------------------------
# "(2019) 12 NWLR (Pt 1685) 1" — the mandatory "(Pt NNNN)" part group is unique to the
# NWLR family and is what makes the citation identifiable at all; Nigeria has no
# widely-used neutral citation, so reporters carry the whole load.
register(Grammar(
    "ng_nwlr_report", "case",
    re.compile(rf"\((?P<year>{_YEAR})\)\s+(?P<vol>\d{{1,3}})\s+(?P<series>NWLR|FWLR)\s*"
               r"\(\s*Pt\.?\s*(?P<part>\d{1,5})\s*\)\s*(?P<page>\d{1,5})", re.IGNORECASE),
    _candidateless,
))

# -- Kenya ------------------------------------------------------------------
# "[2019] eKLR" is a Kenya Law *database* identifier, not a court-issued neutral citation
# and not a page-based reporter — it has no sequence number at all. Recognised so it is
# never mistaken for either, and flagged as database-derived.
register(Grammar(
    "ke_eklr", "case",
    re.compile(rf"\[(?P<year>{_YEAR})\]\s*eKLR\b", re.IGNORECASE),
    _candidateless,
))

# -- Hong Kong --------------------------------------------------------------
# Registry case numbers: "FACV 1/2018", "HCA 18515/1999", "CACV 123/2015". The "/YYYY"
# suffix is the branch signal that separates these from neutral citations. Recognising
# them prevents "HCA 18515/1999" being read as anything else, and preserves the court
# the registry prefix implies.
_HK_PREFIX_ALT = "|".join(sorted(HK_REGISTRY_PREFIXES, key=len, reverse=True))


def _hk_case_number(m: "re.Match[str]") -> Normalised:
    prefix = m.group("prefix").upper()
    if prefix not in HK_REGISTRY_PREFIXES:
        return None, None, DROP
    return None, None, "case"


register(Grammar(
    "hk_case_number", "case",
    re.compile(rf"\b(?P<prefix>{_HK_PREFIX_ALT})\s?(?P<num>\d{{1,6}})/(?P<year>{_YEAR})\b"),
    _hk_case_number,
))


def hk_registry_court(case_number: str) -> str | None:
    """``"FACV 1/2018"`` → ``"HKCFA"`` — the court a registry case number belongs to."""
    m = re.match(rf"\s*({_HK_PREFIX_ALT})", (case_number or "").upper())
    return HK_REGISTRY_PREFIXES.get(m.group(1)) if m else None
