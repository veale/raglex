"""Italian statutory references used in AGCM consumer-protection decisions."""

from __future__ import annotations

import re

from .grammars import _eu_celex
from .models import Citation

CONSUMER_CODE_ID = "it/dlgs/2005/206"

_HOSTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(
        r"\b(?:Codice\s+del\s+consumo|"
        r"(?:decreto\s+legislativo|d\.?\s*lgs\.?)\s*"
        r"(?:6\s+settembre\s+)?2005\s*,?\s*n?\.?\s*206|"
        r"d\.?\s*lgs\.?\s*n?\.?\s*206\s*/\s*2005)\b", re.I),
     CONSUMER_CODE_ID),
    (re.compile(
        r"\b(?:legge\s+10\s+ottobre\s+1990\s*,?\s*n?\.?\s*287|"
        r"legge\s+n?\.?\s*287\s*/\s*1990)\b", re.I),
     "it/legge/1990/287"),
    (re.compile(
        r"\b(?:decreto\s+legislativo|d\.?\s*lgs\.?)\s*"
        r"(?:2\s+agosto\s+)?2007\s*,?\s*n?\.?\s*145\b", re.I),
     "it/dlgs/2007/145"),
    (re.compile(
        r"\b(?:decreto\s+legislativo|d\.?\s*lgs\.?)\s*"
        r"(?:9\s+aprile\s+)?2003\s*,?\s*n?\.?\s*70\b", re.I),
     "it/dlgs/2003/70"),
)

_ARTICLE_BEFORE = re.compile(
    r"(?P<whole>\b(?:articoli?|artt?\.?)\s+"
    r"(?P<run>[0-9][0-9a-z\-]*(?:\s*,\s*(?:comma\s+)?[0-9a-z\-]+)*"
    r"(?:\s+(?:e|ed|nonché)\s+[0-9][0-9a-z\-]*)*)"
    r"(?:\s*,?\s*comma\s+(?P<comma>\d+))?"
    r"(?:\s*,?\s*lettera\s+(?P<letter>[a-z]))?"
    r"\s+(?:del|della|di\s+cui\s+al)\s*)$", re.I,
)


def _pin(article: str, comma: str | None = None, letter: str | None = None) -> str:
    out = f"Articolo {article}"
    if comma:
        out += f", comma {comma}"
    if letter:
        out += f", lettera {letter}"
    return out


_IT_DIRECTIVE_HOST = re.compile(
    r"\b(?:(?P<ucpd>direttiva\s+sulle\s+pratiche\s+commerciali\s+sleali)|"
    r"direttiva\s*(?:\((?:UE|CE|CEE)\)\s*)?"
    r"(?P<a>\d{2,4})\s*/\s*(?P<b>\d{1,4})"
    r"(?P<footnote>1[0-9])?(?:\s*/?\s*(?:UE|CE|CEE))?)\b",
    re.I,
)


def eu_directive_citations(text: str) -> list[Citation]:
    """Italian directive hosts and their governed article lists.

    AGCM PDFs sometimes glue a footnote number to the UCPD number
    (``direttiva 2005/2915``, where 15 is the footnote). That exact observed form
    is treated as Directive 2005/29/EC rather than minting a phantom instrument.
    """
    out: list[Citation] = []
    for host in _IT_DIRECTIVE_HOST.finditer(text):
        a, b = host.group("a"), host.group("b")
        candidate = "32005L0029" if (
            host.group("ucpd")
            or (a == "2005" and (
                (b == "29" and host.group("footnote")) or b == "2915"
            ))
        ) else _eu_celex("directive", a, b)
        if not candidate:
            continue
        out.append(Citation(
            raw=host.group(0), entity_kind="directive", candidate_id=candidate,
            pinpoint=None, char_start=host.start(), char_end=host.end(),
            method="it_eu_directive", confidence=1.0,
        ))
        before_start = max(0, host.start() - 180)
        governed = _ARTICLE_BEFORE.search(text[before_start:host.start()])
        if not governed:
            continue
        run = governed.group("run")
        run_start = before_start + governed.start("run")
        numbers = [
            number for number in re.finditer(r"\d+[a-z]*(?:-[a-z]+)?", run, re.I)
            if not re.search(
                r"(?:comma|lettera)\s*$",
                run[max(0, number.start() - 12):number.start()], re.I)
        ]
        for index, number in enumerate(numbers):
            start = (
                before_start + governed.start("whole")
                if index == 0 else run_start + number.start()
            )
            end = run_start + number.end()
            out.append(Citation(
                raw=text[start:end], entity_kind="directive",
                candidate_id=candidate,
                pinpoint=_pin(
                    number.group(0),
                    governed.group("comma") if index == 0 else None,
                    governed.group("letter") if index == 0 else None,
                ),
                char_start=start, char_end=end,
                method=(
                    "it_eu_directive_article"
                    if index == 0 else "it_eu_directive_article_list"
                ),
                confidence=.99,
            ))
    return out


def italian_citations(text: str) -> list[Citation]:
    out: list[Citation] = eu_directive_citations(text) + data_protection_citations(text)
    for host_re, candidate in _HOSTS:
        for host in host_re.finditer(text):
            before_start = max(0, host.start() - 180)
            before = text[before_start:host.start()]
            article_match = _ARTICLE_BEFORE.search(before)
            if not article_match:
                out.append(Citation(
                    raw=host.group(0), entity_kind="act", candidate_id=candidate,
                    pinpoint=None, char_start=host.start(), char_end=host.end(),
                    method="it_legislation", confidence=.98,
                ))
                continue
            out.append(Citation(
                raw=host.group(0), entity_kind="act", candidate_id=candidate,
                pinpoint=None, char_start=host.start(), char_end=host.end(),
                method="it_legislation", confidence=.98,
            ))
            whole_start = before_start + article_match.start("whole")
            run = article_match.group("run")
            numbers: list[tuple[str, int, int]] = []
            for number in re.finditer(r"\d+[a-z]*(?:-[a-z]+)?", run, re.I):
                prefix = run[max(0, number.start() - 12):number.start()]
                if re.search(r"(?:comma|lettera)\s*$", prefix, re.I):
                    continue
                numbers.append((number.group(0), number.start(), number.end()))
            if not numbers:
                continue
            first = numbers[0][0]
            run_abs = before_start + article_match.start("run")
            first_end = run_abs + numbers[0][2]
            out.append(Citation(
                raw=text[whole_start:first_end], entity_kind="act",
                candidate_id=candidate,
                pinpoint=_pin(first, article_match.group("comma"),
                              article_match.group("letter")),
                char_start=whole_start, char_end=first_end,
                method="it_legislation_article", confidence=.99,
            ))
            # One occurrence per additional member of an article list. Each has its
            # own non-overlapping number span, while the separate host occurrence
            # retains the literal statutory title for audit.
            for number, start, end in numbers[1:]:
                out.append(Citation(
                    raw=number, entity_kind="act", candidate_id=candidate,
                    pinpoint=_pin(number), char_start=run_abs + start,
                    char_end=run_abs + end, method="it_legislation_article_list",
                    confidence=.98,
                ))
    return out


# ---------------------------------------------------------------------------
# Data protection: the Garante's own citation habits
# ---------------------------------------------------------------------------
#
# The vocabulary above is the AGCM's — consumer and competition law. The Garante writes
# a different citation entirely: ``art. 5, par. 1, lett. a), del Regolamento`` and
# ``art. 122 del Codice``, naming each instrument once in full and referring to it by a
# bare noun thereafter. Both bare forms are ambiguous corpus-wide — "il Codice" is the
# Codice del consumo in an AGCM bulletin — so each is only read this way in a document
# that has introduced that instrument.

PRIVACY_CODE_ID = "it/dlgs/2003/196"
GDPR_ID = "32016R0679"

# ``regolamento (UE) 2016/679``, and the two acronyms Italian authors mix freely.
_IT_GDPR_INTRO = re.compile(
    r"\bregolamento\s*\(\s*UE\s*\)\s*2016\s*/\s*679\b|\b(?:RGPD|GDPR)\b", re.I)
# ``d.lgs. 30 giugno 2003, n. 196`` / ``d.lgs. 196/2003`` / the full title.
_IT_PRIVACY_CODE_INTRO = re.compile(
    r"\bcodice\s+in\s+materia\s+di\s+protezione\s+dei\s+dati\s+personali\b|"
    r"\b(?:decreto\s+legislativo|d\.?\s*lgs\.?)\s*(?:30\s+giugno\s+)?2003\s*,?\s*"
    r"n?\.?\s*196\b|\bd\.?\s*lgs\.?\s*n?\.?\s*196\s*/\s*2003\b", re.I)

# ``art. 5, par. 1, lett. a), del Regolamento`` — paragraph, comma and lettered point,
# each optional, each closed by a comma. ``par.`` is the EU-instrument spelling and
# ``comma`` the domestic one; the Garante uses whichever matches the instrument.
_IT_DP_ARTICLE = (
    r"\b(?:artt?\.|articoli?)\s*(?P<article>\d{1,3}(?:-(?:bis|ter|quater|quinquies))?)"
    r"(?:\s*,?\s*(?:par(?:agrafo)?\.?|comma)\s*(?P<para>\d{1,2}))?"
    r"(?:\s*,?\s*(?:lett(?:era)?\.?)\s*(?P<letter>[a-z])\)?)?"
    r"(?:\s*,?\s*(?:punto)\s*(?P<point>\d{1,2})\)?)?"
    r"\s*,?\s*(?:del|dell['’]|della)\s+"
)
_IT_DP_GDPR_RE = re.compile(_IT_DP_ARTICLE + r"(?-i:Regolamento)\b", re.I)
_IT_DP_CODE_RE = re.compile(_IT_DP_ARTICLE + r"(?-i:Codice)\b", re.I)
# The named form, which the Garante uses in the *visto* at the top of every measure and
# whenever a second instrument is in play: "art. 5, par. 1, lett. a) del Regolamento (UE)
# 2016/679" and "art. 9 del regolamento (UE) 2024/1689". Unlike the bare "del
# Regolamento" above it needs no introduction, because it names the instrument itself.
_IT_DP_NUMBERED_RE = re.compile(
    _IT_DP_ARTICLE +
    r"regolamento\s*(?:\(\s*(?:UE|CE|CEE)\s*\)\s*)?(?:n\.?\s*)?"
    r"(?P<a>\d{1,4})\s*/\s*(?P<b>\d{1,4})\b", re.I)
# ``allegato III, punto 2`` / ``punto 2 dell'allegato III`` — the AI Act's whole
# high-risk classification lives in its annexes, so a Garante measure about it that
# could not anchor an allegato reference would link to the instrument and nothing else.
_IT_ANNEX_RE = re.compile(
    # the reverse order first — "il punto 2 dell'allegato III" — so it is not read as a
    # bare allegato reference with the point thrown away
    r"\b(?:(?:punto|n\.)\s*(?P<rpoint>\d{1,3}[a-z]?)\s+(?:del|dell['’])\s*)?"
    r"allegato\s+(?P<annex>[IVXLC]{1,7}|\d{1,2})"
    r"(?:\s*,?\s*(?:punto|n\.)\s*(?P<point>\d{1,3}[a-z]?))?"
    r"\s*,?\s*(?:del|dell['’]|della)\s+"
    r"(?:(?-i:Regolamento)\b|regolamento\s*(?:\(\s*UE\s*\)\s*)?(?:n\.?\s*)?"
    r"(?P<a>\d{1,4})\s*/\s*(?P<b>\d{1,4})\b)", re.I)
# ``considerando 47 del Regolamento`` — the Italian recital, which folds onto the same
# ``Recital N`` anchor the English and Spanish grammars produce.
_IT_RECITAL_RE = re.compile(
    r"\bconsiderando\s*\(?\s*(?P<recital>\d{1,3})\s*\)?"
    r"\s*,?\s*(?:del|dell['’]|della)\s+"
    r"(?:(?-i:Regolamento)\b|regolamento\s*(?:\(\s*UE\s*\)\s*)?(?:n\.?\s*)?"
    r"(?P<a>\d{1,4})\s*/\s*(?P<b>\d{1,4})\b)", re.I)


def _formex_pin(article: str, para: str | None, letter: str | None,
                point: str | None) -> str:
    out = f"Article {article}"
    for part in (para, letter, point):
        if part:
            out += f"({part.casefold()})"
    return out


def _numbered_host(match: re.Match[str]) -> tuple[str, str] | None:
    """The regulation a match named by number, or the GDPR when it wrote "il Regolamento".

    The bare-noun branch is only reachable from a pattern that also accepts the numbered
    form, so it means "this document's Regulation" — and in Garante material that is the
    GDPR unless another one is named, which the numbered branch would then have caught.
    """
    groups = match.groupdict()
    if not groups.get("a"):
        return (GDPR_ID, "regulation")
    celex = _eu_celex("regulation", groups["a"], groups["b"])
    return (celex, "regulation") if celex else None


def data_protection_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for m in _IT_DP_NUMBERED_RE.finditer(text or ""):
        celex = _eu_celex("regulation", m.group("a"), m.group("b"))
        if not celex:
            continue
        out.append(Citation(
            raw=m.group(0), entity_kind="regulation", candidate_id=celex,
            pinpoint=_formex_pin(m.group("article"), m.group("para"),
                                 m.group("letter"), m.group("point")),
            char_start=m.start(), char_end=m.end(),
            method="it_eu_regulation_article", confidence=1.0))
    for pattern, method in ((_IT_ANNEX_RE, "it_annex"), (_IT_RECITAL_RE, "it_recital")):
        for m in pattern.finditer(text or ""):
            host = _numbered_host(m)
            if not host:
                continue
            if host[0] == GDPR_ID and not _IT_GDPR_INTRO.search(text or ""):
                continue          # "del Regolamento" with no Regulation introduced
            groups = m.groupdict()
            if method == "it_annex":
                annex = groups["annex"]
                pinpoint = f"Annex {annex.upper() if annex.isalpha() else annex}"
                point = groups.get("point") or groups.get("rpoint")
                if point:
                    pinpoint += f", point {point}"
            else:
                pinpoint = f"Recital {groups['recital']}"
            out.append(Citation(
                raw=m.group(0), entity_kind=host[1], candidate_id=host[0],
                pinpoint=pinpoint, char_start=m.start(), char_end=m.end(),
                method=method, confidence=.98))
    if _IT_GDPR_INTRO.search(text or ""):
        out += [Citation(
            raw=m.group(0), entity_kind="regulation", candidate_id=GDPR_ID,
            pinpoint=_formex_pin(m.group("article"), m.group("para"),
                                 m.group("letter"), m.group("point")),
            char_start=m.start(), char_end=m.end(),
            method="it_gdpr_article", confidence=.96,
        ) for m in _IT_DP_GDPR_RE.finditer(text)]
    if _IT_PRIVACY_CODE_INTRO.search(text or ""):
        out += [Citation(
            raw=m.group(0), entity_kind="act", candidate_id=PRIVACY_CODE_ID,
            pinpoint=_pin(m.group("article"), m.group("para"), m.group("letter")),
            char_start=m.start(), char_end=m.end(),
            method="it_privacy_code_article", confidence=.96,
        ) for m in _IT_DP_CODE_RE.finditer(text)]
    return out
