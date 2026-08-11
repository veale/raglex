"""Domstolsverket's ``innehall`` — a Swedish judgment as editorial HTML.

Sweden's case-law API delivers the full text, where it delivers it at all, as a single
HTML string built from headings and paragraphs:

```html
<h1>HÖGSTA FÖRVALTNINGSDOMSTOLENS DOM</h1><h1>Mål nr 1889-24</h1>
<h2>BAKGRUND</h2><p>1.&nbsp;&nbsp;&nbsp;&nbsp;När en ny väg ska anläggas …</p>
```

Two features of that markup are load-bearing and both are easy to lose:

**The heading level is the outline.** ``h1`` is the judgment's own top matter (the court's
formula, the case number, SAKEN, the operative order) and ``h2`` the sections of the
reasoning. Flattening both to "a heading" gives a reader a wall of equal-weight lines;
keeping the level gives the outline the court wrote.

**The paragraph number is inside the paragraph, padded with non-breaking spaces.** A
Swedish judgment numbers its reasons "1.", "2.", … and the API pads the number out with
``&nbsp;`` runs rather than with markup. That number is what a later judgment pinpoints
("HFD 2026 ref. 3 p. 12"), so it is lifted out and becomes the segment label, and the
``&nbsp;`` padding is normalised to a single space so the text does not carry four
invisible characters after every number.

Written as a stream parser rather than a tree walk for the reason ``olg_html`` gives: the
strings are editor-produced and occasionally unbalanced, and a judgment lost to a stray
``</p>`` is a judgment the corpus does not hold.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..core.segmentation import assemble
from .base import ParsedDoc, register

#: Elements whose end must break the text run, or paragraphs and cells run together.
_BREAKING = frozenset({"p", "div", "br", "li", "tr", "td", "th", "table", "blockquote",
                       "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dd", "dt"})
_HEADINGS = {"h1": 0, "h2": 1, "h3": 2, "h4": 3, "h5": 4, "h6": 5}
_DROPPED = frozenset({"script", "style", "head"})

#: "1." / "12." opening a paragraph, with the API's ``&nbsp;`` padding already unescaped.
_NUMBER_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+")


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str, str, int]] = []
        self._buf: list[str] = []
        self._level = 0          # outline depth of the heading we are under
        self._heading: str | None = None
        self._in_heading: int | None = None
        self._dropping = 0

    # -- html events ------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROPPED:
            self._dropping += 1
            return
        if tag in _HEADINGS:
            self._flush()
            self._in_heading = _HEADINGS[tag]
        elif tag in _BREAKING:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED:
            self._dropping = max(0, self._dropping - 1)
            return
        if tag in _HEADINGS or tag in _BREAKING:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._dropping:
            self._buf.append(data)

    # -- blocks -----------------------------------------------------------
    def _flush(self) -> None:
        # ``&nbsp;`` arrives already unescaped (convert_charrefs) as U+00A0, which is not
        # whitespace to ``str.split`` — leaving it in put runs of invisible characters
        # between a paragraph number and its first word, so no "1." prefix ever matched.
        text = " ".join("".join(self._buf).replace(" ", " ").split())
        self._buf.clear()
        if not text:
            self._in_heading = None
            return
        if self._in_heading is not None:
            self._level = self._in_heading
            self._heading = text
            self.blocks.append((text, "heading", text, self._in_heading))
            self._in_heading = None
            return
        label = self._heading or "Avgörande"
        kind = "zone"
        if m := _NUMBER_RE.match(text):
            label = f"{int(m.group(1))}."
            kind = "paragraph"
            text = text[m.end():]
        if text:
            self.blocks.append((label, kind, text, self._level + 1))


def parse_domstol_html(data: bytes | str) -> ParsedDoc:
    """A Domstolsverket ``innehall`` string → flat text and outline segments."""
    html = data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    collector = _Collector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # a stream parser must not lose a judgment to bad markup
        pass
    collector._flush()
    text, segments = assemble(collector.blocks)
    zones = [label for label, kind, _body, _level in collector.blocks if kind == "heading"]
    return ParsedDoc(text=text or None, segments=segments,
                     metadata={"zones": zones} if zones else {})


register("domstol-html", parse_domstol_html)
