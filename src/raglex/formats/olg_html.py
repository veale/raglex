"""openlegaldata judgment bodies — the shape both the bulk dump and the live API serve.

Open Legal Data republishes German court decisions with the body in **two** renditions,
and the adapter needs both because neither is always present:

* ``content`` — HTML, the juris house markup the Länder registers publish. Zones are
  ``<h2>`` headings (Tenor, Tatbestand, Entscheidungsgründe…) and each numbered paragraph
  is a ``<dl class="RspDL"><dt><a name="rd_7">7</a></dt><dd>…</dd></dl>``. Newer records
  instead carry an empty ``<rd nr="7"/>`` marker before the paragraph text. This is the
  rendition the **API** serves, and it is the one that carries the Randnummern.
* ``markdown_content`` — a flattened derivative, ``## Zone`` headings and paragraphs in
  a ``N\\n:   text`` definition-list form. Present only in the bulk dump.

The HTML is preferred wherever it exists (99.9% of the dump, and every API response),
because the Randnummer is the citable unit of a German judgment: "BGH, Urt. v. 5.9.2018 –
2 StR 454/17, Rn. 12" points at a paragraph, so a chunk that has lost its number cannot
be cited back. The markdown is the fallback for the handful of rows with no HTML.

Both paths emit the same ``(label, kind, text)`` blocks that ``rii_xml`` does — zone
heading, then one **paragraph** block per Randnummer — so a decision harvested here and
the same decision harvested from rechtsprechung-im-internet segment identically.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..core.segmentation import assemble
from .base import ParsedDoc, register

#: Block-level elements whose end must break the text run, or paragraphs and table
#: cells run together into one unreadable line.
_BREAKING = frozenset({"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3",
                       "h4", "h5", "h6", "dd", "dt", "dl", "table", "blockquote"})
#: Elements whose content is not judgment text at all.
_DROPPED = frozenset({"script", "style", "head"})


class _Collector(HTMLParser):
    """Flattens juris judgment HTML into ``(label, kind, text)`` blocks.

    Deliberately a stream parser rather than a tree walk: the Länder registers publish
    unbalanced markup often enough (stray ``</p>``, unclosed ``<dd>``) that an XML parse
    fails outright on a slice of the corpus, and a decision lost to a stray tag is a
    decision the corpus does not hold.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str, str]] = []
        self._buf: list[str] = []
        self._label: str | None = None     # the Randnummer of the paragraph being read
        self._zone: str | None = None      # the <h2> zone we are inside
        self._in_heading = False
        self._in_dt = False
        self._dt: list[str] = []
        self._drop = 0

    # -- block bookkeeping ---------------------------------------------------
    def _flush(self) -> None:
        body = " ".join("".join(self._buf).split())
        self._buf = []
        if not body:
            self._label = None
            return
        self.blocks.append((self._label or self._zone or "", "paragraph", body))
        self._label = None

    def _emit_zone(self, name: str) -> None:
        # The heading's own text is in the buffer and is NOT a paragraph of the
        # judgment: flushing it first emitted every zone name twice, once as a stray
        # body paragraph and once as the heading.
        self._buf = []
        self._label = None
        name = " ".join(name.split())
        if name:
            self._zone = name
            self.blocks.append((name, "heading", name))

    # -- HTMLParser hooks ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in _DROPPED:
            self._drop += 1
            return
        a = dict(attrs)
        if tag in ("h1", "h2", "h3"):
            self._flush()
            self._in_heading = True
            return
        if tag == "dt":
            # the Randnummer sits in the <dt> of an RspDL pair
            self._flush()
            self._in_dt, self._dt = True, []
            return
        if tag == "dd":
            self._flush()
            self._label = " ".join("".join(self._dt).split()) or None
            return
        if tag == "rd":
            # the newer inline marker: ``<rd nr="7"/>`` opens paragraph 7
            self._flush()
            self._label = (a.get("nr") or "").strip() or None
            return
        if tag == "a" and (a.get("name") or "").startswith("rd_") and not self._in_dt:
            self._flush()
            self._label = (a["name"][3:] or "").strip() or None
            return
        if tag in _BREAKING:
            self._buf.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DROPPED:
            self._drop = max(0, self._drop - 1)
            return
        if tag in ("h1", "h2", "h3"):
            self._emit_zone("".join(self._buf))
            self._buf = []
            self._in_heading = False
            return
        if tag == "dt":
            self._in_dt = False
            return
        if tag in ("dd", "p", "dl", "div", "tr"):
            self._flush()
            return
        if tag in _BREAKING:
            self._buf.append(" ")

    def handle_data(self, data: str) -> None:
        if self._drop:
            return
        if self._in_dt:
            self._dt.append(data)
            return
        self._buf.append(data)

    def close(self) -> None:  # noqa: D102 — flush the tail
        super().close()
        self._flush()


#: A markdown paragraph in the dump's derivative rendition: an optional Randnummer on
#: its own line, then the body behind a ``:`` definition marker.
_MD_PARA = re.compile(r"^(?:(?P<rn>\d{1,5})\n)?:\s{0,4}(?P<body>.*)$")


def _parse_markdown(text: str) -> list[tuple[str, str, str]]:
    """The ``markdown_content`` fallback — ``## Zone`` headings and ``N\\n:   body``
    paragraphs. Used only where a record has no HTML at all."""
    blocks: list[tuple[str, str, str]] = []
    zone: str | None = None
    for chunk in re.split(r"\n{2,}", text or ""):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        heading = re.match(r"^#{1,6}\s*(?P<name>.+?)\s*$", chunk)
        if heading:
            zone = " ".join(heading.group("name").split())
            blocks.append((zone, "heading", zone))
            continue
        m = _MD_PARA.match(chunk)
        if m:
            body = " ".join((m.group("body") or "").split())
            if body:
                blocks.append((m.group("rn") or zone or "", "paragraph", body))
            continue
        body = " ".join(chunk.split())
        if body:
            blocks.append((zone or "", "paragraph", body))
    return blocks


def parse_olg(data: bytes | str, *, markdown: str | None = None) -> ParsedDoc:
    """A decision body → flat text + Randnummer/zone segments.

    ``data`` is the HTML ``content``; ``markdown`` the dump's ``markdown_content``,
    used only when the HTML yields nothing.
    """
    html = data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")
    blocks: list[tuple[str, str, str]] = []
    if html.strip():
        collector = _Collector()
        try:
            collector.feed(html)
            collector.close()
        except AssertionError:  # html.parser's own guard on pathological markup
            blocks = []
        else:
            blocks = collector.blocks
    if not any(kind == "paragraph" for _label, kind, _body in blocks) and markdown:
        blocks = _parse_markdown(markdown)
    text, segments = assemble(blocks)
    zones = [label for label, kind, _ in blocks if kind == "heading"]
    return ParsedDoc(text=text or None, segments=segments,
                     metadata={"zones": zones})


register("olg-html", parse_olg)
