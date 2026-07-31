"""Danish data-protection references — Datatilsynet's vejledninger and afgørelser.

Danish splits the two instruments by the marker that introduces the provision: the GDPR
gets ``artikel 6, stk. 1, litra f``, the domestic Acts get ``§ 8, stk. 2``. Both are
kept, in each instrument's own anchor vocabulary — Formex for the Regulation, the
section sign for the Acts.
"""

from __future__ import annotations

import re

from .models import Citation

GDPR_ID = "32016R0679"
LED_ID = "32016L0680"
# Databeskyttelsesloven (lov nr. 502 af 23. maj 2018) supplements the GDPR;
# retshåndhævelsesloven (lov nr. 410 af 27. april 2017) transposes the Law Enforcement
# Directive. Datatilsynet cites both by name, never by number.
DK_ACTS = {
    "databeskyttelsesloven": "dk:lov:databeskyttelsesloven",
    "retshåndhævelsesloven": "dk:lov:retshaandhaevelsesloven",
    "retshaandhaevelsesloven": "dk:lov:retshaandhaevelsesloven",
}

# ``stk.`` is the paragraph, ``litra`` the lettered point, ``nr.`` the numbered one.
_DK_TAIL = (
    r"(?:\s*,?\s*stk\.?\s*(?P<stk>\d{1,2}))?"
    r"(?:\s*,?\s*litra\s*(?P<litra>[a-z])\)?)?"
    r"(?:\s*,?\s*nr\.?\s*(?P<nr>\d{1,2}))?"
)
# The Regulation is "databeskyttelsesforordningen" or, in the older material and in
# quotations from EU texts, "forordningen"/"GDPR". The bare "forordningen" is only read
# as the GDPR where the full name appears somewhere — otherwise every EU regulation in
# Danish is "forordningen".
_DK_GDPR_HOST = r"(?:databeskyttelsesforordningen|forordningen|(?-i:GDPR))"
_DK_LED_HOST = r"(?:retsh[åa]ndh[æa]velsesdirektivet)"

GDPR_ARTICLE_RE = re.compile(
    r"\bartikel\s*(?P<article>\d{1,3}[a-z]?)" + _DK_TAIL
    + r"(?:\s*,?\s*(?:i|in|af)\s+)?\s*,?\s*" + _DK_GDPR_HOST + r"\b", re.I)
# Danish routinely leaves the instrument implicit in a document that is entirely about
# it ("efter artikel 6, stk. 1, litra f"). Datatilsynet's guidance is single-subject, so
# an article-with-stykke in a document whose title-level vocabulary is the Regulation is
# still the Regulation — but that inference belongs to the carry-forward pass, not here.
LED_ARTICLE_RE = re.compile(
    r"\bartikel\s*(?P<article>\d{1,3}[a-z]?)" + _DK_TAIL
    + r"(?:\s*,?\s*(?:i|af)\s+)?\s*,?\s*" + _DK_LED_HOST + r"\b", re.I)

_DK_ACT_ALT = "|".join(sorted((re.escape(name) for name in DK_ACTS), key=len,
                              reverse=True))
SECTION_RE = re.compile(
    r"§\s*(?P<section>\d{1,3}\s*[a-z]?)" + _DK_TAIL
    + r"\s*,?\s*(?:i\s+)?(?P<act>" + _DK_ACT_ALT + r")\b", re.I)
# …and the mirror order, which is the commoner one: "databeskyttelseslovens § 8".
ACT_SECTION_RE = re.compile(
    r"\b(?P<act>" + _DK_ACT_ALT + r")s?\s+§\s*(?P<section>\d{1,3}\s*[a-z]?)" + _DK_TAIL,
    re.I)

_GDPR_FULL_NAME_RE = re.compile(r"\bdatabeskyttelsesforordningen\b|(?-i:\bGDPR\b)", re.I)


def _formex(match: re.Match) -> str:
    out = f"Article {re.sub(r'\\s+', '', match.group('article'))}"
    for part in (match.group("stk"), match.group("litra"), match.group("nr")):
        if part:
            out += f"({part.casefold()})"
    return out


def _section(match: re.Match) -> str:
    out = f"§ {re.sub(r'\\s+', '', match.group('section'))}"
    if match.group("stk"):
        out += f", stk. {match.group('stk')}"
    if match.group("litra"):
        out += f", litra {match.group('litra').casefold()}"
    if match.group("nr"):
        out += f", nr. {match.group('nr')}"
    return out


def _act_id(name: str) -> str:
    return DK_ACTS[
        next(key for key in DK_ACTS if key.casefold() == name.casefold())]


def danish_citations(text: str) -> list[Citation]:
    if not text:
        return []
    out: list[Citation] = []
    gdpr_named = bool(_GDPR_FULL_NAME_RE.search(text))
    for match in GDPR_ARTICLE_RE.finditer(text):
        # "forordningen" alone is any regulation; require the document to have said
        # which one it means.
        if not gdpr_named and match.group(0).casefold().rstrip().endswith("forordningen"):
            continue
        out.append(Citation(
            raw=match.group(0), entity_kind="regulation", candidate_id=GDPR_ID,
            pinpoint=_formex(match), char_start=match.start(), char_end=match.end(),
            method="dk_gdpr_article", confidence=.97))
    out += [Citation(
        raw=m.group(0), entity_kind="directive", candidate_id=LED_ID,
        pinpoint=_formex(m), char_start=m.start(), char_end=m.end(),
        method="dk_led_article", confidence=.97) for m in LED_ARTICLE_RE.finditer(text)]
    for pattern, method in ((SECTION_RE, "dk_act_section"),
                            (ACT_SECTION_RE, "dk_act_section_prefixed")):
        out += [Citation(
            raw=m.group(0), entity_kind="act", candidate_id=_act_id(m.group("act")),
            pinpoint=_section(m), char_start=m.start(), char_end=m.end(),
            method=method, confidence=.97) for m in pattern.finditer(text)]
    return out
