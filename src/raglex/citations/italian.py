"""Italian statutory references used in AGCM consumer-protection decisions."""

from __future__ import annotations

import re

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


def italian_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
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
