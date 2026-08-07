"""Fact Sheets on the European Union — the Parliament's own explainer of the acquis.

Around 180 sheets, revised every year since the 1990s, each covering one policy area
("Consumer policy: principles and instruments", "Air transport: Single European Sky")
and each opening with a **Legal basis** section that names the instruments it rests on:
"Treaty on the Functioning of the European Union (TFEU): Articles 4(2)(f), 12, 114 and
169". For a corpus that resolves citations, that section alone earns the series its
place.

They are published under their own DTD (``ftu.dtd``) rather than the JATS the briefings
and studies use — a flat run of ``FTU-H1``/``FTU-H2``/``FTU-H3`` headings and ``FTU-P``
paragraphs, with the sheet's identity on the root element: ``SHEET-ID="2.2.1."``,
``AUTHOR``, and a ``DATE`` of ``MM/YYYY``. The headings are flat rather than nested, so
the hierarchy is rebuilt from the heading level as the run is read.
"""

from __future__ import annotations

import re
from datetime import date
from xml.etree import ElementTree as ET

from ..core.segmentation import assemble, localname
from .base import ParsedDoc, register

_HEADING = re.compile(r"^FTU-H([1-6])$", re.I)
#: Apparatus rather than prose: the search-engine blurb duplicates the summary, and the
#: PDF filename is not text.
_DROP = {"seo-description"}


def _text(elem: ET.Element) -> str:
    return " ".join("".join(elem.itertext()).split())


def _month_date(value: str | None) -> date | None:
    """``04/2026`` → the last day the sheet is stated to be current for. A sheet carries
    a month, not a day; taking the first of the month would date it before revisions
    made within it."""
    m = re.match(r"^\s*(\d{1,2})/(\d{4})\s*$", value or "")
    if not m:
        m = re.match(r"^\s*(\d{4})\s*$", value or "")
        return date(int(m.group(1)), 12, 31) if m else None
    month, year = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def parse_ep_factsheet(raw: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ParsedDoc()
    if localname(root.tag).upper() != "FTU-DOCUMENT":
        found = next((e for e in root.iter()
                      if localname(e.tag).upper() == "FTU-DOCUMENT"), None)
        if found is None:
            return ParsedDoc()
        root = found

    title = None
    blocks: list[tuple[str, str, str, int]] = []
    # The headings are siblings of their own paragraphs, so a section is "this heading
    # and everything until the next heading of the same or a higher level".
    label, level, buffer = None, 0, []

    def flush() -> None:
        body = "\n".join(x for x in buffer if x)
        if label and body:
            blocks.append((label[:160], "section", body, max(0, level - 1)))
        elif body:
            blocks.append(("Text", "section", body, 0))
        buffer.clear()

    for node in root:
        name = localname(node.tag).upper()
        if name.lower() in _DROP:
            continue
        if name == "FTU-HEADER":
            title = _text(node) or None
            continue
        if name == "FTU-SUMMARY":
            body = _text(node)
            if body:
                blocks.append(("Summary", "abstract", body, 0))
            continue
        heading = _HEADING.match(name)
        if heading:
            flush()
            label, level = _text(node), int(heading.group(1))
            continue
        text = _text(node)
        if text:
            buffer.append(text)
    flush()

    if not blocks:
        return ParsedDoc()
    text, segments = assemble(blocks)
    metadata = {k.lower().replace("-", "_"): v for k, v in root.attrib.items()
                if k.lower() in ("author", "date", "sheet-id", "internal-number")}
    return ParsedDoc(text=text or None, segments=segments, title=title,
                     decision_date=_month_date(root.get("DATE")),
                     metadata=metadata)


register("ep-factsheet", parse_ep_factsheet)
