"""Formex 4 parser for EU legislation (the Publications Office's native markup).

Why Formex and not AKN4EU: AKN4EU exists in the EU's drafting pipeline, but CELLAR
does not serve it by content negotiation (those requests 400/404). Formex is what
CELLAR reliably returns, and for an act its content member (root ``<ACT>``, *not*
the ``.doc.xml`` bibliographic notice) carries the full ``<ARTICLE>`` structure —
99 articles for the GDPR. We segment per article (``<TI.ART>`` "Article N" +
``<STI.ART>`` heading), the citable unit, so the act becomes a structured,
resolvable node (§5b) with a nicely-renderable hierarchy.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

from ..core.segmentation import assemble, element_text, flow_text, localname
from .base import ParsedDoc, register


def unzip_formex_content(raw: bytes) -> bytes | None:
    """Unpack a CELLAR Formex zip and return the *content* member (root ``ACT`` /
    largest), skipping the ``.doc.xml`` bibliographic notice."""
    if raw[:5] == b"<?xml":
        return raw
    if raw[:2] != b"PK":
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith((".xml", ".fmx"))]
            # prefer non-notice members, then the largest (the enacting terms)
            content = [n for n in members if ".doc." not in n.lower()] or members
            if not content:
                return None
            best = max(content, key=lambda n: zf.getinfo(n).file_size)
            return zf.read(best)
    except zipfile.BadZipFile:
        return None


# Title children (the article's own num + heading) — its label, dropped from the
# body; numbered paragraphs and list points start new lines (read as a list).
_FMX_SKIP = {"ti.art", "sti.art"}
_FMX_LINES = {"parag", "item"}


def _label(article: ET.Element) -> str:
    ti = next((c for c in article.iter() if localname(c.tag) == "TI.ART"), None)
    sti = next((c for c in article.iter() if localname(c.tag) == "STI.ART"), None)
    num = " ".join(element_text(ti).split()) if ti is not None else "Article"
    heading = " ".join(element_text(sti).split()) if sti is not None else ""
    return f"{num} {heading}".strip()


def _recital_blocks(root: ET.Element) -> list[tuple[str, str, str]]:
    """The preamble's recitals (Formex ``CONSID``: ``(N) Whereas …``) — the
    interpretive backbone of an EU instrument, and previously dropped entirely.
    Each becomes its own ``recital`` segment, labelled by its number."""
    blocks: list[tuple[str, str, str]] = []
    for consid in (e for e in root.iter() if localname(e.tag) == "CONSID"):
        no = next((c for c in consid.iter() if localname(c.tag) == "NO.P"), None)
        num = " ".join(element_text(no).split()).strip("()") if no is not None else ""
        label = f"Recital {num}" if num else "Recital"
        body = flow_text(consid, line_tags={"item"})
        body = re.sub(r"^\(\d+\)\s*", "", body)  # drop the leading marker (it's the label)
        if body:
            blocks.append((label, "recital", body))
    return blocks


def parse_formex_legislation(raw: bytes) -> ParsedDoc:
    data = unzip_formex_content(raw)
    if not data:
        return ParsedDoc()
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()

    title = None
    ti = next((e for e in root.iter() if localname(e.tag) == "TITLE"), None)
    if ti is not None:
        title = " ".join(element_text(ti).split()) or None

    articles = [e for e in root.iter() if localname(e.tag) == "ARTICLE"]
    # recitals first (preamble), then the enacting articles — in document order
    blocks: list[tuple[str, str, str]] = _recital_blocks(root)
    blocks += [(_label(a), "article", flow_text(a, skip_tags=_FMX_SKIP, line_tags=_FMX_LINES))
               for a in articles]
    if not blocks:  # not an act we recognise — whole-document text
        blocks = [(title or "document", "section", element_text(root))]

    text, segments = assemble(blocks)
    return ParsedDoc(text=text or None, segments=segments, title=title)


register("formex-legislation", parse_formex_legislation)
