"""GOV.UK "govspeak" publications — the Home Office IPA codes of practice and their kin.

A code of practice is drafted the way legislation is: numbered sections with headings,
and numbered paragraphs beneath them ("3.19."). Practitioners and judgments cite it by
that paragraph number and nothing else. Run through the generic HTML extractor the whole
GOV.UK page came through instead — cookie banner, navigation, contents list, footer —
and the result carried **no segments at all**, so a citation of "paragraph 3.19" had
nothing to land on and the reader could not scroll to it.

The content is already marked up well enough to parse exactly: it lives inside
``<div class="govspeak">``, headings are ``<h2>``/``<h3>``, and every numbered paragraph
is a ``<p>`` opening with its own number. So this parser scopes to the content div and
emits one citable segment per numbered paragraph, under the heading that governs it.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..core.models import Segment
from .base import ParsedDoc, register

# "3.19." / "3.19" / "12.4." at the very start of a paragraph — the citable unit. The
# trailing stop is optional because GOV.UK is not consistent about it, and the number
# may be multi-level ("3.19.2") in the longer codes.
_PARA_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)\.?\s+(?=\S)")
# "1. Introduction" — a numbered heading. The number is part of the citation scheme, so
# it stays in the label; the words are what a reader recognises.
_HEADING_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(.*\S)\s*$")

_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCKS = {"p", "li", "blockquote", "td", "th", "dt", "dd", "figcaption"}
# Page furniture that sits inside the content div in some publications.
_SKIP = {"script", "style", "nav", "form", "button", "svg"}


class _Govspeak(HTMLParser):
    """Collect the govspeak content div as (kind, text) blocks, in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._depth = 0            # nesting depth inside the content div
        self._skip_depth = 0
        self._tag: str | None = None
        self._buf: list[str] = []

    # -- helpers
    def _flush(self) -> None:
        text = re.sub(r"[ \t]+", " ", "".join(self._buf)).strip()
        if text and self._tag:
            self.blocks.append((self._tag, text))
        self._buf, self._tag = [], None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        classes = dict(attrs).get("class") or ""
        if self._depth == 0:
            # The wrapper is emitted twice — an outer component div and an inner
            # ``<div class="govspeak">``. Either is a fine entry point; take the first.
            if tag == "div" and "govspeak" in classes:
                self._depth = 1
            return
        self._depth += 1
        if self._skip_depth:
            return
        if tag in _SKIP:
            self._skip_depth = self._depth
            return
        if tag in _HEADINGS or tag in _BLOCKS:
            self._flush()
            self._tag = "heading" if tag in _HEADINGS else "block"
        elif tag == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0:
            return
        if self._skip_depth and self._depth <= self._skip_depth:
            self._skip_depth = 0
        if tag.lower() in _HEADINGS or tag.lower() in _BLOCKS:
            self._flush()
        self._depth -= 1
        if self._depth == 0:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._depth and not self._skip_depth and self._tag:
            self._buf.append(data)


def _label_for(kind: str, text: str) -> tuple[str, str] | None:
    """``(label, segment kind)`` for a block, or ``None`` if it is body prose."""
    if kind == "heading":
        match = _HEADING_NUM_RE.match(text)
        return (text if match else text), "heading"
    match = _PARA_NUM_RE.match(text)
    # "para 3.19" is the form a judgment uses, and the form the anchor key folds.
    return (f"para {match.group(1)}", "paragraph") if match else None


def parse_govspeak(data: bytes) -> ParsedDoc:
    parser = _Govspeak()
    try:
        parser.feed(data.decode("utf-8", "replace"))
        parser.close()
    except Exception:  # noqa: BLE001 — malformed markup yields whatever was collected
        pass
    if not parser.blocks:
        return ParsedDoc()

    pieces: list[str] = []
    segments: list[Segment] = []
    open_label: str | None = None
    open_kind = "paragraph"
    open_start = 0
    cursor = 0

    def close(end: int) -> None:
        nonlocal open_label
        if open_label is not None and end > open_start:
            segments.append(Segment(open_label, open_start, end, kind=open_kind,
                                    level=0 if open_kind == "heading" else 1))
        open_label = None

    for kind, text in parser.blocks:
        started = _label_for(kind, text)
        if started:
            close(cursor)
            open_label, open_kind = started
            open_start = cursor
        pieces.append(text)
        cursor += len(text) + 2          # the "\n\n" joined in below
    close(cursor)

    body = "\n\n".join(pieces)
    # The last block's span must not run past the text (cursor counts a trailing join).
    segments = [
        Segment(s.label, s.char_start, min(s.char_end, len(body)), kind=s.kind,
                level=s.level)
        for s in segments if s.char_start < len(body)
    ]
    return ParsedDoc(text=body or None, segments=segments)


register("govuk-govspeak", parse_govspeak)
