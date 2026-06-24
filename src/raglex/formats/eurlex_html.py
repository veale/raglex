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


def parse_eurlex_html(data: bytes) -> ParsedDoc:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    title = None
    t = soup.find("title")
    if t:
        title = t.get_text(strip=True) or None

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
