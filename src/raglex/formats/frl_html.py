"""Federal Register of Legislation (Commonwealth of Australia) content parser.

The FRL is an OData API (see ``adapters/au_legislation.py``), but the API serves the
*document bytes* as an EPUB, and the register also unzips that EPUB to server-rendered
HTML at a deterministic path — which is the clean, no-JavaScript route to the text (the
Angular front-end pages are client-rendered and useless to a scraper; the unzipped EPUB
HTML is plain Word-exported markup).

The markup is Word-style: every block is a ``<p class="…">`` whose class carries the
structural role. Verified against the *Acts Interpretation Act 1901* compilation:

* ``ActHead2`` — a Part/Division heading (``CharPartNo`` + ``CharPartText``).
* ``ActHead5`` — a **section** heading; ``CharSectno`` holds the section number.
* ``subsection`` / ``paragraph`` / ``Definition`` / ``Tabletext`` … — body blocks.
* ``TOC2`` / ``TOC5`` — the contents list at the top (dropped — it just repeats the
  headings and would double every section).
* ``ENote*`` — the **endnotes**, which on the FRL carry the amendment history table
  ("Endnote 4 — Amendment history"). Lifted into ``metadata["endnotes"]`` rather than
  the body, the same way the LRC annotations are split out for Ireland, so the
  operative text reads clean.

Segments follow the Part → section hierarchy so a retrieval hit maps to a citable
provision, and the FRL's structured amendment graph (from the API's ``statusHistory``)
means the endnote table is a bonus, not the primary edge source.
"""

from __future__ import annotations

import re
from html import unescape

from ..core.models import Segment
from ..core.segmentation import SEP
from .base import ParsedDoc, register

_TAG_RE = re.compile(r"<[^>]+>")
_P_RE = re.compile(r'<(?P<tag>[pP])\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>', re.S)
_CLASS_RE = re.compile(r'class="([^"]*)"', re.I)
_SECTNO_RE = re.compile(r'class="CharSectno"[^>]*>(.*?)</span>', re.S | re.I)

# Block class → (kind, level). Parts/Divisions are containers; ActHead5 is a section.
_PART = {"acthead2", "acthead1", "actheadp"}
_DIV = {"acthead3", "acthead4"}
_SECTION = {"acthead5"}
_TITLE = {"charact", "actno", "actname"}


def _text(html: str) -> str:
    return unescape(_TAG_RE.sub("", html)).replace("\xa0", " ")


def _norm(s: str) -> str:
    return " ".join(s.split())


def parse_frl_html(data: bytes) -> ParsedDoc:
    html = data.decode("utf-8", errors="replace")
    # The unzipped-EPUB path 404s for the very latest compilation until its static HTML
    # is generated, and the register then answers with its Angular front-end shell (a
    # SPA page whose text is navigation + "the requested title could not be loaded"),
    # NOT the document. Storing that boilerplate as the Act's text would be worse than
    # storing nothing, so any shell marker aborts the parse and the adapter falls back.
    if any(marker in html for marker in (
            "_ngcontent", "_nghost", "<frl-", "requested title could not be loaded",
            "404 - File or directory")):
        return ParsedDoc()

    title: str | None = None
    blocks: list[tuple[str, str, str, int]] = []  # label, kind, text, level
    endnotes: list[str] = []
    label, kind, level = "document", "section", 0
    buffer: list[str] = []
    in_endnotes = False

    def flush() -> None:
        text = "\n".join(b for b in buffer if b).strip()
        if text:
            blocks.append((label, kind, text, level))
        buffer.clear()

    for m in _P_RE.finditer(html):
        cls = ""
        cm = _CLASS_RE.search(m.group("attrs"))
        if cm:
            cls = cm.group(1).strip().lower().split()[0] if cm.group(1).strip() else ""
        body = _norm(_text(m.group("body")))
        if not body:
            continue

        # The contents list at the top repeats every heading — skip it entirely.
        if cls.startswith("toc"):
            continue

        # Endnotes carry the amendment history; hold them separately from the law.
        if cls.startswith("enoteshead") or (not in_endnotes and cls.startswith("enote")):
            in_endnotes = True
        if in_endnotes:
            endnotes.append(body)
            continue

        if title is None and (cls in _TITLE or (not blocks and body.isupper() and len(body) > 8)):
            title = body

        if cls in _PART or cls in _DIV:
            flush()
            label, kind, level = body, ("part" if cls in _PART else "chapter"), 0
            buffer.append(body)
        elif cls in _SECTION:
            flush()
            sm = _SECTNO_RE.search(m.group("body"))
            num = _norm(_text(sm.group(1))) if sm else ""
            label = f"s. {num} {body[len(num):].strip() if num and body.startswith(num) else body}".strip()
            kind, level = "section", 1
            buffer.append(body)
        else:
            buffer.append(body)
    flush()

    if not blocks:
        return ParsedDoc(title=title)

    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for lbl, knd, text, lvl in blocks:
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=lbl, char_start=cursor, char_end=cursor + len(text),
                                kind=knd, level=lvl))
        parts.append(text)
        cursor += len(text)

    return ParsedDoc(
        text=SEP.join(parts) or None,
        segments=segments,
        title=title,
        metadata={"endnotes": endnotes[:400]},  # amendment-history prose (bounded)
    )


register("frl-html", parse_frl_html)
