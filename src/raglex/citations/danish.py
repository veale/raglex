"""Danish provisions, numbered EU instruments and domestic docket/report references.

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
    out: list[Citation] = general_danish_citations(text)
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


# Numbered EU acts retain the shared CELEX year/number safeguards; only the
# Danish drafting vocabulary and provision ladder differ.
_EU_ACT_RE = re.compile(
    r"\b(?:(?:artikel\s*(?P<article>\d{1,3}[a-z]?)" + _DK_TAIL
    + r"\s*,?\s*(?:(?:i|af)\s+)?)?)"
    r"(?:(?:Europa-Parlamentets\s+og\s+)?Rådets\s+|Kommissionens\s+)?"
    r"(?P<kind>forordning(?:en|ens)?|direktiv(?:et|ets)?|afgørelse(?:n|ns)?)\s+"
    r"(?:\((?:EU|EF|EØF|Euratom)\)\s*)?(?:nr\.?\s*)?"
    r"(?P<a>\d{2,4})/(?P<b>\d{1,4})(?:/(?:EU|EF|EØF|Euratom))?\b", re.I)
# Some judgments print the number and adoption date instead of number/year.
_MONTHS = {name: i for i, name in enumerate(
    ['januar','februar','marts','april','maj','juni','juli','august','september','oktober','november','december'], 1)}
_EU_DATED_RE = re.compile(_EU_ACT_RE.pattern.split(r'(?P<a>')[0]
                         + r'(?P<number>\d{1,4})\s+af\s+(?P<day>\d{1,2})\.?\s+'
                         + r'(?P<month>' + '|'.join(_MONTHS) + r')\s+(?P<year>\d{4})\b', re.I)
_DOCKET_RE = re.compile(r"\b(?:BS|SS|AS|FS|SKS)-\d{1,8}/(?:19|20)\d{2}-[A-ZÆØÅ]{2,8}\b")
_REPORT_RE = re.compile(r"\bU\.?\s*(?P<year>(?:19|20)\d{2})[. ](?P<page>\d{1,5})\s*(?P<court>H|V|Ø)\b")
_TREATIES = {'TEUF': '12012E', 'TEU': '12012M'}
_TREATY_RE = re.compile(r'\b(?:artikel\s*(?P<article>\d{1,3}[a-z]?)' + _DK_TAIL
                        + r'\s*,?\s*(?:(?:i|af)\s+)?)?(?P<treaty>TEUF|TEU)\b')


def general_danish_citations(text):
    from .grammars import _eu_celex, _plausible_eu_year
    from datetime import date
    out = []
    for m in _EU_ACT_RE.finditer(text):
        word = m['kind'].lower()
        kind = 'regulation' if word.startswith('forordning') else 'directive' if word.startswith('direktiv') else 'decision'
        candidate = _eu_celex(kind, m['a'], m['b'])
        if candidate:
            out.append(Citation(m[0], kind, candidate, _formex(m) if m['article'] else None,
                                m.start(), m.end(), 'dk_eu_instrument', .97))
    for m in _EU_DATED_RE.finditer(text):
        # A dated domestic afgørelse has the same word: require EU provenance.
        if not re.search(r'\((?:EU|EF|EØF|Euratom)\)|Rådets|Kommissionens', m[0], re.I):
            continue
        if not _plausible_eu_year(m['year']) or int(m['number']) == 0:
            continue
        try:
            date(int(m['year']), _MONTHS[m['month'].lower()], int(m['day']))
        except ValueError:
            continue
        word = m['kind'].lower()
        kind, descriptor = ('regulation', 'R') if word.startswith('forordning') else (
            ('directive', 'L') if word.startswith('direktiv') else ('decision', 'D'))
        out.append(Citation(m[0], kind, f"3{m['year']}{descriptor}{int(m['number']):04d}",
                            _formex(m) if m['article'] else None,
                            m.start(), m.end(), 'dk_eu_dated_instrument', .97))
    for m in _TREATY_RE.finditer(text):
        out.append(Citation(m[0], 'treaty', _TREATIES[m['treaty']], _formex(m) if m['article'] else None,
                            m.start(), m.end(), 'dk_eu_treaty', .97))
    for m in _DOCKET_RE.finditer(text):
        out.append(Citation(m[0], 'case', 'dk/docket/' + m[0], None,
                            m.start(), m.end(), 'dk_docket', .95))
    for m in _REPORT_RE.finditer(text):
        out.append(Citation(m[0], 'case', f"dk/ufr/{m['year']}/{m['page']}/{m['court'].lower()}", None,
                            m.start(), m.end(), 'dk_report', .9))
    return out


DANISH_ARTICLE = r"artikel\s*\d{1,3}[a-z]?(?:\s*,?\s*stk\.?\s*\d{1,2})?(?:\s*,?\s*litra\s*[a-z]\)?)?(?:\s*,?\s*nr\.?\s*\d{1,2})?"
_DK_ARTICLE_RE = re.compile(r'\bartikel\s*(?P<article>\d{1,3}[a-z]?)' + _DK_TAIL, re.I)


def danish_article_pinpoint(raw):
    m = _DK_ARTICLE_RE.fullmatch(raw.strip())
    return _formex(m) if m else None
