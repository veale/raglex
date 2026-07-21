"""LegalDocML.de (LDML.de) parser — the German national profile of Akoma Ntoso.

German legal documents on rechtsinformationen.bund.de are published as **LDML.de**,
which is Akoma Ntoso with German metadata blocks and hierarchy labels. Because the
element *local names* are the AKN-standard ones (``act``/``body``/``article``/
``paragraph``/``num``/``heading`` plus the hierarchy containers ``book``/``part``/
``title``/``chapter``/``section``), the existing AKN machinery does the structural
heavy lifting; this module only adds the German-specific bits:

- the German container names AKN doesn't carry (``book`` = *Buch*, ``subtitle`` =
  *Untertitel*, ``division`` = *Gliederung*) as heading levels;
- German unit labels — a *§* (``num`` "§ 1") is the citable unit, not an English
  "s. 1", so labels are taken verbatim; sub-units (*Absatz* → ``paragraph``, and the
  *Satz* inside it) each start a new line so a provision reads as a list (§6b);
- the ELI and the *Jurabk* abbreviation (BGB, SGB, GG, BDSG…) lifted from the FRBR /
  proprietary metadata so ``de_neuris`` can key by ELI and by the familiar short name.

Point-in-time is a known gap in the beta (only current versions are reachable by ELI),
so no version chain is built here — that is left for when the portal ships it.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from ..core.models import Segment
from ..core.segmentation import SEP, element_text, flow_text, localname
from .akoma_ntoso import _relations
from .base import ParsedDoc, register

# German hierarchy containers → header lines (num + heading), body lives in children.
_HEADING_TAGS = {"book", "part", "subpart", "title", "subtitle", "chapter",
                 "section", "subsection", "division", "crossheading"}
# Leaf citable units — a § (article) or a stand-alone paragraph.
_UNIT_TAGS = {"article", "paragraph"}
# Pass-through wrappers we descend into without emitting.
_PASS_TAGS = {"akomantoso", "act", "bill", "doc", "documentcollection", "body",
              "mainbody", "hcontainer", "container", "preface", "preamble"}
# A unit's own num/heading are its label; sub-units start a new line.
_SKIP = {"num", "heading", "marker", "authorialnote"}
_LINES = {"paragraph", "subparagraph", "point", "list", "item", "intro"}


def _child_text(elem: ET.Element, name: str) -> str | None:
    child = next((c for c in elem if localname(c.tag).lower() == name), None)
    if child is None:
        return None
    return " ".join(element_text(child).split()) or None


def _label(elem: ET.Element) -> str:
    """A German unit's label from its num + heading ("§ 1 Beginn der Rechtsfähigkeit")."""
    num = _child_text(elem, "num") or ""
    heading = _child_text(elem, "heading") or ""
    return f"{num} {heading}".strip() or localname(elem.tag)


def _heading_only(elem: ET.Element) -> str:
    return f"{_child_text(elem, 'num') or ''} {_child_text(elem, 'heading') or ''}".strip()


def _walk(elem: ET.Element, level: int, blocks: list[tuple[str, str, str, int]]) -> None:
    for child in elem:
        name = localname(child.tag).lower()
        if name in _UNIT_TAGS:
            # a paragraph directly under a § is a sub-unit of it, not a leaf — but at
            # body level a bare paragraph is its own unit; either way emit its flow text.
            text = flow_text(child, skip_tags=_SKIP, line_tags=_LINES)
            if text.strip():
                blocks.append((_label(child), "section", text, level))
        elif name in _HEADING_TAGS:
            header = _heading_only(child)
            if header:
                blocks.append((header, name, header, level))
            _walk(child, level + 1, blocks)
        elif name in _PASS_TAGS:
            _walk(child, level, blocks)


def _clean_text(elem: ET.Element) -> str:
    """Text of a title element, excluding ``authorialNote``/``marker`` subtrees — an
    LDML.de ``docTitle`` wraps its short name plus a transposition ``authorialNote``
    ("Dieses Gesetz dient der Umsetzung…"), and only the name is the title."""
    parts: list[str] = []

    def visit(e: ET.Element) -> None:
        if localname(e.tag).lower() in ("authorialnote", "marker"):
            return
        if e.text and e.text.strip():
            parts.append(e.text.strip())
        for c in e:
            visit(c)
            if c.tail and c.tail.strip():
                parts.append(c.tail.strip())

    visit(elem)
    return " ".join(parts).strip()


def _title(root: ET.Element) -> str | None:
    # docTitle/longTitle hold the official name (Langüberschrift); shortTitle is the
    # abbreviation, so it is NOT a title (it becomes the jurabk instead).
    for name in ("docTitle", "longTitle"):
        for e in root.iter():
            if localname(e.tag) == name:
                txt = _clean_text(e)
                if txt:
                    return txt
    for e in root.iter():
        if localname(e.tag) == "FRBRname" and e.get("value"):
            return e.get("value")
    return None


_ELI_RE = re.compile(r"(eli/[^\s?#\"']+)", re.IGNORECASE)


def ldml_eli(root: ET.Element) -> str | None:
    """The ELI from the FRBRWork (…/FRBRthis/@value = 'eli/bund/bgbl-1/…')."""
    for e in root.iter():
        if localname(e.tag) != "FRBRWork":
            continue
        for child in e.iter():
            if localname(child.tag) in ("FRBRthis", "FRBRuri"):
                m = _ELI_RE.search(child.get("value") or "")
                if m:
                    return m.group(1).rstrip("/")
    return None


def ldml_jurabk(root: ET.Element) -> str | None:
    """The *Jurabk* abbreviation (BGB, SGB V, SaatG…). LDML.de puts it in ``shortTitle``
    ("(SaatG)") — surrounding parentheses stripped — or a proprietary jurabk/alias block."""
    for e in root.iter():
        name = localname(e.tag).lower()
        if name in ("jurabk", "amtabk"):
            txt = " ".join(element_text(e).split())
            if txt:
                return txt
        if localname(e.tag) == "FRBRalias" and "abk" in (e.get("name") or "").lower():
            if e.get("value"):
                return e.get("value")
    for e in root.iter():
        if localname(e.tag) == "shortTitle":
            txt = " ".join(element_text(e).split()).strip("()").strip()
            if txt:
                return txt
    return None


def parse_ldml_de(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()

    blocks: list[tuple[str, str, str, int]] = []
    _walk(root, 0, blocks)
    if not blocks:  # unrecognised shape — whole-document fallback
        blocks = [(_title(root) or "Dokument", "section", element_text(root), 0)]

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

    return ParsedDoc(
        text=SEP.join(parts) or None,
        segments=segments,
        relations=_relations(root),  # AKN <ref> extraction reused as-is
        title=_title(root),
        metadata={"eli": ldml_eli(root), "jurabk": ldml_jurabk(root)},
    )


register("ldml-de", parse_ldml_de)
