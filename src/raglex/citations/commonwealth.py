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

from ..formats.lims_xml import ca_id
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


# -- Canadian legislation ---------------------------------------------------
# Canadian judgments cite statutes by three formal, unambiguous forms, and each of these
# identifies a *federal* instrument that the Justice Laws import holds. Only the
# chapter-coded forms are handled here: a bare "the Patent Act" is ambiguous between the
# federal Act and a provincial one of the same name, so it is left to the held-title
# resolver (facade.match_named_legislation), which links a name only when exactly one
# held Act carries it.
#
# The pinpoint ("s. 8 of …") is captured where it leads the citation so the edge lands on
# the exact provision, matching how the UK statute grammars record section anchors.
_CA_PINPOINT = (r"(?:s(?:ection|s|\.)?\.?\s*(?P<sec>\d+[\w.()]*)"
                r"(?:\s+of\s+(?:the\s+)?[A-Z][^,;.]{0,60}?)?,?\s*)?")


def _ca_section(m: "re.Match[str]", cid: str | None) -> Normalised:
    sec = m.groupdict().get("sec")
    return cid, (f"s. {sec}" if sec else None), "act"


# Consolidated: "R.S.C. 1985, c. C-46", "R.S.C., 1985, c. A-1", "R.S., c. C-46" (pre-1985).
# The letter-dashed chapter code (C-46) IS the consolidated id, so this resolves directly
# to ca/act/c-46. Requiring the letter-dash form is what separates federal R.S.C. chapters
# from provincial "R.S.O. 1990, c. P.33" (letter-DOT) and from the annual number-only form.
def _ca_rsc(m: "re.Match[str]") -> Normalised:
    return _ca_section(m, ca_id("act", m.group("code")))


register(Grammar(
    "ca_statute_consolidated", "act",
    re.compile(
        _CA_PINPOINT +
        r"R\.?S\.?C?\.?(?:,?\s*\d{4})?,?\s*c\.?\s*(?P<code>[A-Z]{1,2}-[\d.]+)\b"
    ),
    _ca_rsc,
))

# Annual: "S.C. 2019, c. 18" (English), "L.C. 2019, ch. 18" (French). The annual chapter
# number is NOT the consolidated id, so this stays candidate-less and resolves via the
# annual→consolidated alias the ca-federal import mints. The trailing ", c. N" is what
# separates it from "1999 S.C. 583" (Session Cases, a Scottish reporter).
register(Grammar(
    "ca_statute_annual", "act",
    re.compile(_CA_PINPOINT + r"(?:S\.?C\.?|L\.?C\.?)\s*\d{4},?\s*(?:c|ch)\.?\s*\d+\b"),
    lambda m: _ca_section(m, None),
))

# Regulations: "SOR/2018-69", "DORS/2018-69" (French), "SI/2005-91", "TR/2005-91". SOR/SI
# are the English series, DORS/TR the French names for the same instruments.
_CA_REG_SERIES = {"sor": "SOR", "dors": "SOR", "si": "SI", "tr": "SI"}


def _ca_reg(m: "re.Match[str]") -> Normalised:
    series = _CA_REG_SERIES[m.group("series").lower()]
    return ca_id("regulation", f"{series}/{m.group('num')}"), None, "regulation"


register(Grammar(
    "ca_regulation", "regulation",
    re.compile(r"\b(?P<series>SOR|DORS|SI|TR)/(?P<num>\d{2,4}-\d+)\b"),
    _ca_reg,
))


# -- Australian legislation -------------------------------------------------
# Australian judgments cite statutes as "<Title> Act <year> (<Juris>)" — "Migration Act
# 1958 (Cth)", "s 61 of the Crimes Act 1900 (NSW)". The registers publish the act NUMBER,
# not the citation, so there is no id to build from the text; instead this stays a
# name-only reference that facade.match_named_legislation resolves against the *titles* of
# the Australian legislation actually harvested (au-cth, au-nsw, …).
#
# The jurisdiction tag is deliberately CONSUMED (not just look-ahead asserted) for two
# reasons: it makes this match longer than the generic UK "<Title> Act <year>" grammar, so
# the extractor's longest-match dedupe prefers it; and it keeps an Australian citation off
# the UK statute gazetteer, which would otherwise mis-resolve "Companies Act 2006 (Cth)" to
# the UK Companies Act. reference_key() strips the trailing tag back off before matching a
# held title, so "Fair Work Act 2009 (Cth)" still lands on the held "Fair Work Act 2009".
AU_JURISDICTION_TAGS = ("Cth", "Commonwealth", "NSW", "Vic", "Qld", "WA", "SA",
                        "Tas", "ACT", "NT")
_AU_JURIS = "|".join(AU_JURISDICTION_TAGS)


def _au_statute(m: "re.Match[str]") -> Normalised:
    sec = m.groupdict().get("sec")
    # Name-only: no id yet (the citation carries no act number), resolved by title against
    # harvested Australian legislation. Not routed through any gazetteer.
    return None, (f"s. {sec}" if sec else None), "act"


register(Grammar(
    "au_statute_named", "act",
    re.compile(
        r"(?:s(?:ection|s|\.)?\.?\s*(?P<sec>\d+[A-Za-z]?(?:\(\d+[A-Za-z]?\))*)\s+of\s+)?"
        r"(?:the\s+)?"
        r"(?P<title>[A-Z][A-Za-z0-9'’.\-]*"
        r"(?:,?\s+(?:and|of|for|to|in|on|the|[A-Z][A-Za-z0-9'’.\-]*|\([^()]{1,40}\)))*?"
        r"\s+Act)\s+(?P<year>(?:18|19|20)\d{2})"
        rf"\s*\((?P<juris>{_AU_JURIS})\)"
    ),
    _au_statute,
))
