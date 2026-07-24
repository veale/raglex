"""BWB parser — Dutch consolidated legislation (wetten.overheid.nl "toestand" XML).

The Netherlands publishes consolidated law as BWB XML (Basis Wetten Bestand), not
Akoma Ntoso: ``<wetgeving>`` → ``<hoofdstuk>`` → ``<artikel>`` (``<kop>`` with
``<nr>`` + ``<titel>``, then ``<lid>``/``<al>``). We segment per *artikel* (the
citable unit), with chapter/paragraaf headings carrying the hierarchy level (§6b),
so NL statute renders in the same structured reader and the raw BWB stays the
machine-readable base.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ..core.models import Segment
from ..core.segmentation import SEP, element_text, flow_text, localname
from .base import ParsedDoc, register

_HEADING_TAGS = {"hoofdstuk", "afdeling", "paragraaf", "titeldeel", "boek", "deel"}
_UNIT_TAGS = {"artikel"}
_PASS_TAGS = {"toestand", "wetgeving", "wettekst", "wet-besluit", "body", "regeling",
              "regeling-tekst", "circulaire", "circulaire-tekst"}
# Annotation subtrees carried inside the content (Juriconnect change notes, source data,
# editorial corrections) — pruned before rendering so their dates/numbers ("2001 584
# 18-12-2001") don't bleed into the article body or the title.
_DROP_TAGS = {"meta-data", "kadata", "redactie", "bwb-inputbestand", "bwb-wijzigingen",
              "redactionele-correcties", "wetsdelen"}
# Sub-units that each start a new line, so a numbered provision reads as a list instead of
# one flat wall: a "lid" (numbered member) and a "li" (enumerated list item). NB: "al"
# (alinea) is deliberately NOT here — an <al> is the body of its lid/li, and breaking before
# it would strip the "1"/"1°." marker onto its own line.
_LINE_TAGS = {"lid", "li"}


def _prune(elem: ET.Element) -> None:
    """Recursively remove annotation subtrees (`_DROP_TAGS`) so only enacted content
    remains. ElementTree has no parent links, so we drop matching children at each level."""
    for child in list(elem):
        if localname(child.tag).lower() in _DROP_TAGS:
            elem.remove(child)
        else:
            _prune(child)


def _kop_label(elem: ET.Element, prefix: str = "") -> str:
    kop = next((c for c in elem if localname(c.tag) == "kop"), None)
    if kop is None:
        return prefix or localname(elem.tag)
    nr = next((c for c in kop.iter() if localname(c.tag) == "nr"), None)
    titel = next((c for c in kop.iter() if localname(c.tag) == "titel"), None)
    parts = [prefix] if prefix else []
    if nr is not None:
        parts.append(" ".join(element_text(nr).split()))
    if titel is not None:
        parts.append(" ".join(element_text(titel).split()))
    return " ".join(p for p in parts if p).strip() or localname(elem.tag)


def _title(root: ET.Element) -> str | None:
    for name in ("citeertitel", "intitule"):
        el = next((e for e in root.iter() if localname(e.tag) == name), None)
        if el is not None:
            txt = " ".join(element_text(el).split())
            if txt:
                return txt
    return None


def _walk(elem: ET.Element, level: int, blocks: list[tuple[str, str, str, int]]) -> None:
    for child in elem:
        name = localname(child.tag).lower()
        if name in _UNIT_TAGS:
            # Render the article body like law: omit the <kop> label (it's the segment
            # label) and start each <lid>/<li> on its own line (§6b), instead of the flat
            # space-joined blob element_text() produced.
            text = flow_text(child, skip_tags={"kop"}, line_tags=_LINE_TAGS)
            if text.strip():
                blocks.append((_kop_label(child, "Artikel"), "article", text, level))
        elif name in _HEADING_TAGS:
            header = _kop_label(child, name.capitalize())
            if header:
                blocks.append((header, "hoofdstuk", header, level))
            _walk(child, level + 1, blocks)
        elif name in _PASS_TAGS:
            _walk(child, level, blocks)


def parse_bwb(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()
    _prune(root)   # strip change-note / source-data annotations before rendering
    blocks: list[tuple[str, str, str, int]] = []
    _walk(root, 0, blocks)
    if not blocks:
        blocks = [(_title(root) or "document", "article", element_text(root), 0)]

    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for label, kind, text, level in blocks:
        text = text.strip()
        if not text:
            continue
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=label, char_start=cursor, char_end=cursor + len(text),
                                kind=kind, level=level))
        parts.append(text)
        cursor += len(text)
    return ParsedDoc(text=SEP.join(parts) or None, segments=segments, title=_title(root))


register("bwb", parse_bwb)
