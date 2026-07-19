"""Akoma Ntoso / LegalDocML parser (the standards-aligned machine-readable base).

Akoma Ntoso is the open standard the UK already publishes (legislation.gov.uk
``data.akn`` and Find Case Law), so one parser serves UK **legislation** and
**judgments**. It preserves the document's own hierarchy — Part → Chapter →
Section → subsection for Acts; numbered paragraphs for judgments — as ``Segment``s
with a ``level`` (§6b), which is exactly what a structured, nicely-formatted reader
renders from, while the raw AKN remains the canonical machine-readable store.
"""

from __future__ import annotations

import re
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

# Schedules live in <hcontainer name="schedule">, and their citable units are
# <paragraph>, not <section>. Without this the units were labelled by the generic
# "section" rule and a schedule came out as "s. 1", "s. 2" … — restarting the
# Act's own numbering (the Children Act 1989 appears to run s.108 → s.1) and
# colliding with the real sections for pinpoint matching. OSCOLA cites these as
# "sch 1 para 1", or "sch 1 pt 1 para 1" where the schedule is divided into Parts.
#
# NB the case-insensitivity is scoped to the KEYWORD only. A blanket re.I would
# make [A-Z] match lower case too, and legislation.gov.uk runs the enabling-section
# note straight onto the number — "SCHEDULE 1Section 15(1)." would then capture
# "1Section". The trailing (?![a-z]) is what stops the number eating the next word.
_SCHEDULE_NUM_RE = re.compile(r"(?i:SCHEDULE)\s*([0-9]+[A-Z]{0,2}|[A-Z]{1,2}[0-9]*)(?![a-z])")
_PART_NUM_RE = re.compile(r"(?i:PART)\s*([0-9]+[A-Z]{0,2}|[IVXLC]+)(?![a-z])")


def _child_text(elem: ET.Element, name: str) -> str | None:
    child = next((c for c in elem if localname(c.tag).lower() == name), None)
    if child is None:
        return None
    return " ".join(element_text(child).split()) or None


def _label(elem: ET.Element, kind: str, ctx: dict | None = None) -> str:
    num = _child_text(elem, "num") or ""
    heading = _child_text(elem, "heading") or ""
    label = f"{num} {heading}".strip()
    # inside a schedule, cite OSCOLA-style: "Sch 1 para 1" / "Sch 1 Pt 1 para 1"
    if ctx and ctx.get("schedule"):
        pin = f"Sch {ctx['schedule']}"
        if ctx.get("part"):
            pin += f" Pt {ctx['part']}"
        if num:
            pin += f" para {num.strip()}"
        return f"{pin} {heading}".strip() if heading else pin
    if kind == "section" and num and not num.lower().startswith(("s", "art")):
        label = f"s. {label}"
    return label or kind


def _schedule_of(elem: ET.Element) -> str | None:
    """The schedule's number from its <num> ("SCHEDULE 1Section 15(1)." → "1").
    legislation.gov.uk runs the enabling-section note straight onto the number, so
    the digits have to be picked out rather than read off whole."""
    m = _SCHEDULE_NUM_RE.search(_child_text(elem, "num") or "")
    return m.group(1) if m else None


def _part_of(elem: ET.Element) -> str | None:
    m = _PART_NUM_RE.search(_child_text(elem, "num") or "")
    return m.group(1) if m else None


def _heading_only(elem: ET.Element) -> str:
    """A container's own header (num + heading), excluding its child sections."""
    num = _child_text(elem, "num") or ""
    heading = _child_text(elem, "heading") or ""
    return f"{num} {heading}".strip()


def _frbr_work_id(data: bytes) -> str | None:
    """The legislation URI path from an AKN file's FRBRWork ("…/id/ukpga/2006/46"
    → "ukpga/2006/46"), so a manual upload keys under the same id a harvest would.
    Reads the WORK (not Expression/Manifestation), whose URI omits the version date."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    for e in root.iter():
        if localname(e.tag) != "FRBRWork":
            continue
        for child in e:
            if localname(child.tag) in ("FRBRthis", "FRBRuri"):
                m = re.search(r"legislation\.gov\.uk/(?:id/)?([a-z]{2,6}/[^\s?#\"]+)",
                              child.get("value") or "", re.IGNORECASE)
                if m:
                    return m.group(1).rstrip("/")
    return None


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


def _walk(elem: ET.Element, level: int, blocks: list[tuple[str, str, str, int]],
          ctx: dict | None = None) -> None:
    ctx = ctx or {}
    for child in elem:
        name = localname(child.tag).lower()
        if name in _UNIT_TAGS:
            text = flow_text(child, skip_tags=_AKN_SKIP, line_tags=_AKN_LINES)
            if text.strip():
                kind = "paragraph" if ctx.get("schedule") else "section"
                blocks.append((_label(child, "section", ctx), kind, text, level))
        elif name in _HEADING_TAGS:
            header = _heading_only(child)
            if header:
                # a Part heading inside a schedule is named per schedule, so two
                # schedules that both open with "Part I General" stay distinct
                lab = f"Sch {ctx['schedule']} {header}" if ctx.get("schedule") else header
                blocks.append((lab, name, header, level))
            # a Part only qualifies a pinpoint when it divides a SCHEDULE; a Part of
            # the Act's body doesn't appear in a section citation ("s 5", not "pt 2 s 5")
            sub = dict(ctx, part=_part_of(child)) if (name == "part" and ctx.get("schedule")) else ctx
            _walk(child, level + 1, blocks, sub)
        elif name in _PASS_TAGS:
            # <hcontainer> carries its role in @name: a schedule opens a new
            # pinpoint context, a crossheading is just a heading
            role = (child.get("name") or "").lower()
            if role == "schedule":
                header = _heading_only(child)
                num = _schedule_of(child)
                if header:
                    # "SCHEDULE 1Section 15(1). Financial Provision" is how the
                    # source runs the enabling note into the number; show the
                    # schedule by its number and name instead
                    heading = _child_text(child, "heading") or ""
                    lab = f"Sch {num} {heading}".strip() if num else header
                    blocks.append((lab, "schedule", header, level))
                _walk(child, level + 1, blocks, dict(ctx, schedule=num, part=None))
            else:
                _walk(child, level, blocks, ctx)


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
