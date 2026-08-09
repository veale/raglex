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

# An Act's operative unit is <section>; a STATUTORY INSTRUMENT's is
# <hcontainer name="regulation"> (or "rule"/"article"), which has no dedicated AKN tag.
# Those fell through to the generic pass-through, so the walk descended past the
# regulation and segmented its child <paragraph> elements instead — every SI in the
# corpus came out as a run of "s. (1)", "s. (2)", "s. (1A)" …, carrying no regulation
# number at all and repeating those same labels once per regulation. PECR held 237 such
# segments, so no citation of "regulation 6" could ever land on anything.
#
# The unit is named the way its own instrument is cited: OSCOLA pinpoints an SI by
# "reg 6", rules of court by "r 3.1".
_HCONTAINER_UNITS = {
    "regulation": "reg.", "rule": "r.", "article": "art.", "section": "s.",
}

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


def _label(elem: ET.Element, kind: str, ctx: dict | None = None,
           unit: str = "s.") -> str:
    # An Act's <num> is "1"; an SI's is "1." — the same provision number, punctuated
    # differently by the source. Drop the trailing stop so one instrument's labels don't
    # read "reg. 6." while another's read "s. 6".
    num = (_child_text(elem, "num") or "").strip().rstrip(".").strip()
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
    if kind == "section" and num and not num.lower().startswith(("s", "art", "reg", "r.")):
        label = f"{unit} {label}"
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


def expression_valid_from(data: bytes) -> str | None:
    """The date the EXPRESSION in ``data`` is the law as at, or None.

    legislation.gov.uk stamps every rendition it serves with
    ``<FRBRdate date="2026-06-19" name="validFrom"/>`` in its FRBRExpression, and names
    the same date in the manifestation URI (``/ukpga/2018/12/2026-06-19/data.akn``).
    That is precisely the "as at" a reader needs: the revised text is continuously
    maintained, so "the current text" is only meaningful with the date it was current on.

    Read the EXPRESSION, not the Work (which carries enactment) or the Manifestation
    (whose ``transform`` date is merely when the XML was rendered, and moves whenever the
    publisher re-renders an unchanged act).
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    for e in root.iter():
        if localname(e.tag) != "FRBRExpression":
            continue
        for child in e:
            if localname(child.tag) == "FRBRdate" and child.get("name") == "validFrom":
                d = (child.get("date") or "")[:10]
                return d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None
    return None


def _title(root: ET.Element) -> str | None:
    # legislation.gov.uk's Dublin Core title is the publisher's canonical, already
    # whitespace-correct display title. Prefer it to reconstructing <longTitle> from
    # adjacent <p> elements: XML text concatenation otherwise produces defects such as
    # ``2016on`` and ``Councilof`` when the source has no literal inter-element space.
    dc_ns = "{http://purl.org/dc/elements/1.1/}"
    for e in root.iter():
        if e.tag == f"{dc_ns}title":
            txt = " ".join(element_text(e).split())
            if txt:
                return txt
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


#: Unit element → the ``kind`` its segments are recorded under. Everything not named
#: here keeps the historical "section", so Acts, SIs and judgments are unaffected.
#:
#: An ARTICLE is not a section, and saying so had a visible cost: the UK GDPR's segments
#: are labelled "Article 15" and anchor correctly, but they were typed "section", so
#: asking that document for ``outline_kind='article'`` returned an empty list and the
#: articles could only be found by asking for sections. The parser was applying UK-Act
#: typing to a UK-hosted assimilated EU instrument.
_UNIT_KINDS = {"article": "article"}


def _unit_kind(name: str, ctx: dict) -> str:
    # Inside a schedule the citable unit is the paragraph, whatever the element is called.
    if ctx.get("schedule"):
        return "paragraph"
    return _UNIT_KINDS.get(name, "section")


#: Amending text quotes the instrument it changes. Everything under these carries the
#: OTHER document's structure and must never be read as this document's own.
_QUOTED_TAGS = {"quotedstructure", "embeddedstructure"}


def _contains_unquoted(elem: ET.Element, tag: str) -> bool:
    """Is ``tag`` present under ``elem``, ignoring quoted/embedded structure subtrees?"""
    for child in elem:
        name = localname(child.tag).lower()
        if name in _QUOTED_TAGS:
            continue
        if name == tag or _contains_unquoted(child, tag):
            return True
    return False


def _article_led(root: ET.Element) -> bool:
    """Does this instrument's body cite by ARTICLE rather than by section?

    An assimilated EU regulation does, and it also divides its chapters into
    ``<section>`` elements — "Section 1 Transparency and modalities" — whose children are
    the articles. That collides with a UK Act, where ``<section>`` IS the citable unit
    and is emitted whole without descending. So in the UK GDPR the parser stopped at
    Chapter III's four sections and never reached Articles 12 to 23: 57 of ~99 articles
    were indexed, and no citation of Article 15 could land on anything.

    The instrument itself says which it is. Where articles are present, a section is a
    grouping heading; where they are not, nothing changes.

    QUOTED articles don't count. An amending schedule reproduces the text it inserts
    inside ``<quotedStructure>`` — "in Article 4 substitute—" — and that quoted matter
    is the OTHER instrument's structure, not this one's. The Data Protection Act 2018
    has 271 sections and three articles, all three quoted inside one schedule
    paragraph; on the un-scoped test it read as article-led, so every one of its
    sections was demoted to a grouping heading and emitted as its own title with no
    body. The whole Act rendered as a table of contents.
    """
    body = None
    for elem in root.iter():
        if localname(elem.tag).lower() in ("body", "mainbody"):
            body = elem
            break
    scope = body if body is not None else root
    return _contains_unquoted(scope, "article")


def _walk(elem: ET.Element, level: int, blocks: list[tuple[str, str, str, int]],
          ctx: dict | None = None, *, units: frozenset[str] | set[str] = _UNIT_TAGS,
          headings: frozenset[str] | set[str] = _HEADING_TAGS) -> None:
    ctx = ctx or {}
    for child in elem:
        name = localname(child.tag).lower()
        if name in units:
            text = flow_text(child, skip_tags=_AKN_SKIP, line_tags=_AKN_LINES)
            if text.strip():
                kind = _unit_kind(name, ctx)
                blocks.append((_label(child, "section", ctx), kind, text, level))
        elif name in headings:
            header = _heading_only(child)
            if header:
                # a Part heading inside a schedule is named per schedule, so two
                # schedules that both open with "Part I General" stay distinct
                lab = f"Sch {ctx['schedule']} {header}" if ctx.get("schedule") else header
                blocks.append((lab, name, header, level))
            # a Part only qualifies a pinpoint when it divides a SCHEDULE; a Part of
            # the Act's body doesn't appear in a section citation ("s 5", not "pt 2 s 5")
            sub = dict(ctx, part=_part_of(child)) if (name == "part" and ctx.get("schedule")) else ctx
            _walk(child, level + 1, blocks, sub, units=units, headings=headings)
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
                _walk(child, level + 1, blocks, dict(ctx, schedule=num, part=None),
                      units=units, headings=headings)
            elif (role in _HCONTAINER_UNITS and role in units
                  and not ctx.get("schedule")):
                # An SI's regulation: a citable unit in its own right, so emit it whole
                # rather than descending to its sub-paragraphs. (Inside a schedule the
                # paragraph rule above already governs, and must keep doing so.)
                text = flow_text(child, skip_tags=_AKN_SKIP, line_tags=_AKN_LINES)
                if text.strip():
                    blocks.append((
                        _label(child, "section", ctx, unit=_HCONTAINER_UNITS[role]),
                        _unit_kind(role, ctx), text, level))
            else:
                _walk(child, level, blocks, ctx, units=units, headings=headings)


def parse_akn(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()

    blocks: list[tuple[str, str, str, int]] = []
    # An article-led instrument's <section> is a chapter subdivision, not a unit.
    if _article_led(root):
        _walk(root, 0, blocks, units=_UNIT_TAGS - {"section"},
              headings=_HEADING_TAGS | {"section"})
    else:
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
