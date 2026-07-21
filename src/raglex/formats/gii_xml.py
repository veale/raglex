"""juris "gii-norm" XML parser — the legacy gesetze-im-internet legislation format.

gesetze-im-internet.de publishes every federal statute as a per-law XML file against
the juris ``gii-norm`` DTD (the bulk seed source behind ``de-gii``). It predates
LDML.de and looks nothing like AKN, so it needs its own parser — but it maps onto the
same ``ParsedDoc`` (text + segments + title) as everything else.

Shape (from real files): a ``<dokumente>`` root holds many ``<norm>`` elements. The
first norm is the law header — ``<jurabk>`` (BGB, ZApprO…), ``<langue>`` (the long
title), ``<ausfertigung-datum>``, ``<fundstelle>``. Each following norm is a provision:
``<enbez>`` is the citable unit ("§ 1", "Inhaltsübersicht"), ``<titel>`` its heading,
and the body lives in ``<textdaten><text><Content>`` as ``<P>`` paragraphs (or a
``<TOC>`` table for the contents overview). ``<gliederungseinheit>`` blocks carry the
Abschnitt/Unterabschnitt hierarchy headings.

Each § becomes a native chunk ``Segment`` (§6b), the German analogue of AKN sections —
so the older bulk and the newer LDML.de feed produce the same structured records.
"""

from __future__ import annotations

from datetime import date
from xml.etree import ElementTree as ET

from ..core.models import Segment
from ..core.segmentation import SEP, element_text, flow_text, localname
from .base import ParsedDoc, register

# A provision's own num/heading are its label; sub-points start a new line.
_SKIP = {"enbez", "titel"}
_LINES = {"P", "DT", "DD", "LA"}


def _text(elem: ET.Element | None, tag: str) -> str | None:
    if elem is None:
        return None
    child = next((c for c in elem.iter() if localname(c.tag) == tag), None)
    if child is None:
        return None
    return " ".join(element_text(child).split()) or None


def _iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_gii(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()

    norms = [n for n in root.iter() if localname(n.tag) == "norm"]
    if not norms:
        return ParsedDoc()

    header = norms[0]
    meta0 = next((c for c in header if localname(c.tag) == "metadaten"), header)
    title = _text(meta0, "langue") or _text(meta0, "jurabk")
    jurabk = _text(meta0, "jurabk")
    doc_date = _iso(_text(meta0, "ausfertigung-datum"))

    blocks: list[tuple[str, str, str, int]] = []
    for norm in norms[1:]:
        meta = next((c for c in norm if localname(c.tag) == "metadaten"), None)
        textdaten = next((c for c in norm if localname(c.tag) == "textdaten"), None)
        enbez = _text(meta, "enbez")
        titel = _text(meta, "titel")
        # Abschnitt/Unterabschnitt hierarchy header → its own header line
        gl_bez = _text(meta, "gliederungsbez")
        gl_titel = _text(meta, "gliederungstitel")
        if gl_bez:
            head = " ".join(x for x in (gl_bez, gl_titel) if x)
            blocks.append((head, "section-heading", head, 0))

        text_el = None
        if textdaten is not None:
            text_el = next((c for c in textdaten.iter() if localname(c.tag) == "text"), None)
        # skip the giant Inhaltsübersicht TOC table body — keep only its label
        is_toc = text_el is not None and any(localname(c.tag) == "TOC" for c in text_el.iter())
        body = "" if (is_toc or text_el is None) else flow_text(
            text_el, skip_tags=_SKIP, line_tags=_LINES)
        label = " ".join(x for x in (enbez, titel) if x) or enbez or "Vorschrift"
        if body.strip():
            blocks.append((label, "section", body, 1))
        elif enbez and not gl_bez:
            # a provision with no body (repealed/placeholder) still gets a node
            blocks.append((label, "section", titel or enbez, 1))

    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for label, kind, text, level in blocks:
        text = (text or "").strip()
        if not text:
            continue
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=label, char_start=cursor, char_end=cursor + len(text),
                                kind=kind, level=level))
        parts.append(text)
        cursor += len(text)

    return ParsedDoc(
        text=SEP.join(parts) or None,
        segments=segments,
        title=title,
        decision_date=doc_date,
        metadata={"jurabk": jurabk, "doknr": header.get("doknr")},
    )


register("gii-xml", parse_gii)
