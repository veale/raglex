"""EUR-Lex HTML parser for EU legislation.

CELLAR won't serve Akoma Ntoso for legislation and its Formex is multi-file, so
the reliable machine-readable rendition for EU acts is the EUR-Lex HTML, which
marks articles with ``ti-art`` / ``sti-art`` classes. We segment on those article
boundaries (the citable unit) and keep the title. Less rich than AKN, but it makes
the act a real node — so every "interprets 32016R0679" edge resolves (§5b) — and
gives an article-structured reader. A proper Formex-legislation parser can later
register under a different format name without touching callers.
"""

from __future__ import annotations

import re

from ..core.models import Segment
from ..core.segmentation import SEP
from .base import ParsedDoc, register

_ARTICLE_RE = re.compile(r"^Article\s+\d+", re.IGNORECASE)

# The <title> of an EUR-Lex page is frequently just "EUR-Lex - 31995L0046 - EN".
# For older instruments served in the legacy "Avis juridique important" layout the
# real name is in the body instead:
#
#     <h1>31995L0046</h1>
#     <p><strong>Directive 95/46/EC of the European Parliament … </strong>
#        <em><br>Official Journal L 281 , 23/11/1995 P. 0031 - 0050</em></p>
#
# Without this, ~3,000 EU instruments (Directive 95/46 among them) fell through to
# a CELEX-derived placeholder — "Directive 1995/46" — even though their proper
# title was sitting in the HTML all along.
_CELEX_H1_RE = re.compile(r"^[1-9]\d{4}[A-Z]{1,2}\d+$")
# An act's title always names the instrument somewhere near its front. Matching on
# the keyword rather than on a fixed opener is deliberate: real titles begin
# "Council Directive…", "First Commission Directive…", "COUNCIL IMPLEMENTING
# REGULATION…", and an allowlist of openers misses more than it catches.
_INSTRUMENT_RE = re.compile(
    r"\b(?:Directive|Regulation|Decision|Recommendation|Opinion|Agreement|"
    r"Protocol|Convention|Joint\s+Action|Common\s+Position)\b", re.IGNORECASE)
# a page <title> that is really a placeholder: the CELEX banner, or the OJ's
# own XML filename ("L_2011296EN.01000301.xml")
_GENERIC_PAGE_TITLE = re.compile(
    r"^\s*(?:EUR-?Lex\b.*|[A-Z]_\d[\w.]*\.xml|ANNEX|Document\s+\d\w+)\s*$", re.IGNORECASE)


def _looks_like_act_title(text: str | None) -> bool:
    """Is this plausibly an instrument's name rather than page furniture?"""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if not (20 <= len(t) <= 700):
        return False
    if _GENERIC_PAGE_TITLE.match(t):
        return False
    return bool(_INSTRUMENT_RE.search(t[:120]))


def _clean(text: str | None) -> str | None:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t or None


def _title_from_body(soup) -> str | None:
    """The instrument's real name, hunted through EUR-Lex's several page layouts.

    No single element carries it across the corpus, so this is an ordered ladder:
    the legacy "Avis juridique important" pages put it in a <p> under an <h1> that
    holds the CELEX; the modern Official Journal rendition splits it across
    <p class="oj-doc-ti"> lines; the portal wrapper keeps it only in a meta tag.
    """
    # 1. meta tags — present on the legacy (DC.description) and portal
    #    (WT.z_docTitle) layouts, and unambiguous where present
    for name in ("DC.description", "WT.z_docTitle"):
        tag = soup.find("meta", attrs={"name": name})
        cand = _clean(tag.get("content")) if tag else None
        if _looks_like_act_title(cand):
            return cand

    # 2. legacy body: <h1>31995L0046</h1> followed by the title paragraph. The
    #    <em> sibling holds the OJ reference, so prefer the <strong> when present.
    for h1 in soup.find_all("h1"):
        if not _CELEX_H1_RE.match(h1.get_text(strip=True) or ""):
            continue
        p = h1.find_next("p")
        if p is None:
            continue
        strong = p.find("strong") or p.find("b")
        cand = _clean((strong or p).get_text(" ", strip=True))
        if _looks_like_act_title(cand):
            return cand

    # 3. modern OJ rendition: the name, date and subject are consecutive
    #    <p class="oj-doc-ti"> lines and only read as a title once joined
    parts = [_clean(p.get_text(" ", strip=True))
             for p in soup.find_all("p", class_="oj-doc-ti")]
    joined = " ".join(p for p in parts if p)
    if _looks_like_act_title(joined):
        return joined
    return None


def parse_eurlex_html(data: bytes) -> ParsedDoc:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    t = soup.find("title")
    title = _clean(t.get_text(strip=True)) if t else None
    # A placeholder page <title> — the CELEX banner "EUR-Lex - 31995L0046 - EN", or
    # the OJ's own XML filename — hides a real name elsewhere on the page. ~3,000 EU
    # instruments were being stored under a CELEX-derived stand-in ("Directive
    # 1995/46") for want of looking.
    if not title or _GENERIC_PAGE_TITLE.match(title):
        title = _title_from_body(soup) or title

    # Article-structured pass: EUR-Lex uses class names containing 'ti-art'.
    blocks: list[tuple[str, str]] = []  # (label, text)
    current_label: str | None = None
    current: list[str] = []

    def flush():
        if current_label and current:
            blocks.append((current_label, "\n".join(current).strip()))

    paras = soup.find_all(["p", "div"])
    for p in paras:
        classes = " ".join(p.get("class") or [])
        text = p.get_text(" ", strip=True)
        if not text:
            continue
        is_article = "ti-art" in classes or _ARTICLE_RE.match(text)
        if is_article and len(text) < 40:  # an "Article N" heading line
            flush()
            current_label = text
            current = []
        elif current_label is not None:
            current.append(text)
    flush()

    if not blocks:  # no article markers — fall back to flat body text
        body = soup.get_text("\n", strip=True)
        return ParsedDoc(text=body or None, title=title)

    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for label, text in blocks:
        if not text:
            continue
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=label, char_start=cursor, char_end=cursor + len(text),
                                kind="article", level=0))
        parts.append(text)
        cursor += len(text)
    return ParsedDoc(text=SEP.join(parts) or None, segments=segments, title=title)


register("eurlex-html", parse_eurlex_html)
