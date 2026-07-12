"""Akoma Ntoso / LegalDocML parser (the standards-aligned machine-readable base).

Akoma Ntoso is the open standard the UK already publishes (legislation.gov.uk
``data.akn`` and Find Case Law), so one parser serves UK **legislation** and
**judgments**. It preserves the document's own hierarchy — Part → Chapter →
Section → subsection for Acts; numbered paragraphs for judgments — as ``Segment``s
with a ``level`` (§6b), which is exactly what a structured, nicely-formatted reader
renders from, while the raw AKN remains the canonical machine-readable store.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ..core.models import (
    ExtractedVia,
    RelationshipType,
    ResolutionStatus,
    Segment,
    TypedRelation,
)
from ..core.segmentation import SEP, element_text, flow_text, localname

# A unit's own num + heading are its label (dropped from the body); subsections,
# lettered levels and points each start a new line so provisions read as a list.
_AKN_SKIP = {"num", "heading"}
_AKN_LINES = {"subsection", "paragraph", "subparagraph", "level", "point", "item"}
from .base import ParsedDoc, register

# Container headings we descend through, emitting just their num+heading as a
# header line (the body lives in their child sections).
_HEADING_TAGS = {"part", "chapter", "subpart", "title", "crossheading"}
# Leaf citable units — emit the whole element's text, don't descend further.
_UNIT_TAGS = {"section", "article", "rule", "regulation", "paragraph", "judgmentbody"}
# Pass-through wrappers.
_PASS_TAGS = {"akomantoso", "act", "bill", "doc", "judgment", "body", "mainbody", "hcontainer"}


def _child_text(elem: ET.Element, name: str) -> str | None:
    child = next((c for c in elem if localname(c.tag).lower() == name), None)
    if child is None:
        return None
    return " ".join(element_text(child).split()) or None


def _label(elem: ET.Element, kind: str) -> str:
    num = _child_text(elem, "num") or ""
    heading = _child_text(elem, "heading") or ""
    label = f"{num} {heading}".strip()
    if kind == "section" and num and not num.lower().startswith(("s", "art")):
        label = f"s. {label}"
    return label or kind


def _heading_only(elem: ET.Element) -> str:
    """A container's own header (num + heading), excluding its child sections."""
    num = _child_text(elem, "num") or ""
    heading = _child_text(elem, "heading") or ""
    return f"{num} {heading}".strip()


def _title(root: ET.Element) -> str | None:
    # Prefer a human title (UK AKN's FRBRname is the citation "2000 c. 36").
    for name in ("shortTitle", "docTitle", "FRBRalias", "longTitle"):
        for e in root.iter():
            if localname(e.tag) == name:
                txt = " ".join(element_text(e).split())
                if name == "FRBRalias":
                    txt = e.get("value") or txt
                if txt.strip():
                    return txt
    for e in root.iter():
        if localname(e.tag) == "FRBRname" and e.get("value"):
            return e.get("value")
    return None


def _relations(root: ET.Element) -> list[TypedRelation]:
    """External citations only (cross-Act / EU / case refs); internal section
    cross-references (`#section-5`) are dropped as noise. Deduped.

    An href is only worth an edge when a candidate id can be derived from it
    (a legislation.gov.uk path, a caselaw URI, a CELEX inside an eur-lex URL).
    Underivable footnote links — the National Archives eu-exit webarchive wrappers
    around uriserv:OJ.… references especially — minted tens of thousands of
    permanently-unresolvable pending edges that buried the manual worklist."""
    from ..resolve.matchers import first_candidate

    seen: dict[str, None] = {}
    rels: list[TypedRelation] = []
    for e in root.iter():
        if localname(e.tag) != "ref":
            continue
        href = (e.get("href") or "").strip()
        if not href.startswith("http"):
            continue
        if not any(k in href for k in ("legislation.gov.uk", "eur-lex", "europa.eu", "caselaw")):
            continue
        if href in seen:
            continue
        if first_candidate(href) is None:
            continue  # no derivable target — a dead footnote link, not a citation
        seen[href] = None
        rels.append(
            TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string=href,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            )
        )
    return rels


def _walk(elem: ET.Element, level: int, blocks: list[tuple[str, str, str, int]]) -> None:
    for child in elem:
        name = localname(child.tag).lower()
        if name in _UNIT_TAGS:
            text = flow_text(child, skip_tags=_AKN_SKIP, line_tags=_AKN_LINES)
            if text.strip():
                blocks.append((_label(child, "section"), "section", text, level))
        elif name in _HEADING_TAGS:
            header = _heading_only(child)
            if header:
                blocks.append((header, name, header, level))
            _walk(child, level + 1, blocks)
        elif name in _PASS_TAGS:
            _walk(child, level, blocks)


def parse_akn(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()

    blocks: list[tuple[str, str, str, int]] = []
    _walk(root, 0, blocks)
    if not blocks:  # unrecognised shape — fall back to whole-document text
        blocks = [(_title(root) or "document", "section", element_text(root), 0)]

    # assemble flat text + leveled segments (offsets account for the SEP joiner)
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
        relations=_relations(root),
        title=_title(root),
    )


register("akoma-ntoso", parse_akn)
