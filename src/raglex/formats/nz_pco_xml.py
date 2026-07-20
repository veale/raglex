"""New Zealand PCO legislation XML parser — shape-inferring, schema-verified.

The Parliamentary Counsel Office publishes NZ legislation in a **PCO-specific,
DTD-defined schema**. This parser was originally written blind (the website is
bot-walled and the Developer API key was pending), so it **infers structure from
shape** rather than hard-coding element names:

* a **unit** (a citable provision) is any element carrying a numbering child
  (``label``/``num``/``number``) — that is what makes a provision addressable;
* a **container** (Part, Subpart, Schedule, cross-heading) is any element carrying a
  heading child but no numbering of its own;
* everything else is descended through.

**Verified against live PCO XML on 2026-07-20** (Income Tax Act 2007, 20MB, the largest
act in the corpus). The shape inference held: Parts, Subparts and provisions all came out
correctly labelled. Two things it could not have known are now encoded here.

**1. Most of a PCO file is not the law.** In the Income Tax Act, 3.0M of 8.5M characters
are editorial apparatus, and naively ingesting it puts ~40% non-operative text into
retrieval and embeddings — every section trailing a wall of "inserted, on 1 April 2008,
by section 307 of…". These subtrees are pruned (``_NON_OPERATIVE``):

* ``notes/history/history-note`` — amendment annotations (14,308 in the ITA alone);
* ``ird.aids``/``term.list`` — Inland Revenue indexing markers, which otherwise strand
  bare keywords ("income year tax") mid-sentence;
* ``cf`` — comparative references to the 2004 Act;
* ``contents`` — per-Part tables of contents, duplicating headings;
* ``end/skeletons`` — the text of *amending* acts, which is not this act's text at all.

**2. The amendment notes are structured data, and worth keeping.** They are pruned from
the body but recovered as ``AMENDED_BY`` relations rather than discarded — see
``amendment_relations``.

The **fallback** still stands: if nothing structural is recognised, the whole document's
text is captured as one block, so an unexpected schema costs structure, never content.
``metadata["inferred_structure"]`` records which path ran.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree as ET

from ..core.models import (
    ExtractedVia,
    RelationshipType,
    ResolutionStatus,
    Segment,
    TypedRelation,
)
from ..core.segmentation import SEP, flow_text, localname
from .base import ParsedDoc, register

# Numbering children — their presence is what marks an element as a citable provision.
_LABELS = {"label", "num", "number", "no"}
# Heading children — a container announces itself with one of these and no numbering.
_HEADINGS = {"heading", "title", "crosshead", "crosshead.text", "shorttitle"}
# Known PCO container names, used as hints alongside the shape inference.
_CONTAINER_HINTS = {"part", "subpart", "schedule", "sched", "crosshead", "group",
                    "chapter", "division", "book"}
# Known PCO provision names.
_UNIT_HINTS = {"prov", "provision", "subprov", "section", "sec", "clause", "cl",
               "article", "rule", "reg", "regulation"}
# Structural wrappers descended through silently.
_PASS_HINTS = {"legislation", "act", "bill", "sop", "amendment-paper", "instrument",
               "body", "front", "cover", "main", "contents", "schedules", "prelim"}
# Sub-units that start a new line so enumerated provisions read as a list. `label` is
# deliberately absent: a subsection's number should open its own line ("1 This Act comes
# into force…") rather than be stranded on a line of its own. `subprov.crosshead` is the
# PCO's mid-provision heading, which otherwise runs into the preceding sentence.
_LINES = {"subprov", "label-para", "def-para", "subprov.crosshead", "crosshead",
          "proviso", "item", "list-item"}

# Editorial apparatus, pruned from the body before the structure walk. Everything here
# is *about* the law rather than being it; see the module docstring for the measured cost
# of leaving it in. Pruning is recursive because `notes` also nests inside `para`.
_NON_OPERATIVE = {"notes", "cf", "ird.aids", "term.list", "contents", "skeletons",
                  "end.reprint-note"}

_DOCTYPE_RE = re.compile(r"<!DOCTYPE.*?(?:\[.*?\])?\s*>", re.S)
# "(2007 No 109)" closing an amendment note — the amending act's year and number, which
# map directly onto the PCO work-id grammar and so onto a corpus stable_id.
_AMENDING_ACT_RE = re.compile(r"\((\d{4})\s*No\s*(\d+)\)")
# "(with effect on 1 April 2008)" — a retrospective amendment, where the legal effect
# predates the amending act. Worth flagging: it changes what the law *was*.
_RETROSPECTIVE_RE = re.compile(r"\(with effect on ([^)]+)\)")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"[ \t]+([,;:.\)\]])")
_ENTITY_DECL_RE = re.compile(r'<!ENTITY\s+(?P<name>[A-Za-z_][\w.-]*)\s+"(?P<value>[^"]*)"\s*>')
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def nz_id(typ: str, subtype: str, year: str | int, number: str,
          language: str = "en", version_date: str | None = None) -> str:
    """The corpus stable_id for an NZ work: ``nz/act/public/1990/109``.

    Built from the PCO's own six-segment identifier grammar (type, subtype, year, number,
    language, version date), which the API and the website URLs share — so the id is the
    register's key rather than an invented one. The version date is appended only when
    pinning a specific point-in-time Expression; the bare form is the Work.

    Ephemeral segments (PCO prefixes them ``~`` when it lacks the real value, e.g. an
    unknown commencement date) are preserved verbatim, because an id that silently drops
    the marker would look permanent when it is not.
    """
    parts = [str(p).strip() for p in (typ, subtype, year, number) if str(p).strip()]
    base = "nz/" + "/".join(parts).lower()
    if language and language.lower() not in ("", "en"):
        base = f"{base}/{language.lower()}"
    return f"{base}@{version_date}" if version_date else base


def expand_entities(text: str) -> str:
    """Substitute entities declared in the document's own internal DTD subset, then drop
    the DOCTYPE. The PCO schema is DTD-defined, and ElementTree ignores internal subsets
    while hard-failing on the first undefined entity — so without this a single declared
    entity costs the entire document. Nothing external is ever fetched (no XXE surface),
    matching the Irish parser's approach ([[irish-legislation]])."""
    doctype = _DOCTYPE_RE.search(text)
    if not doctype:
        return text
    entities = {m.group("name"): m.group("value")
                for m in _ENTITY_DECL_RE.finditer(doctype.group(0))}
    body = text[:doctype.start()] + text[doctype.end():]
    for name, value in entities.items():
        body = body.replace(f"&{name};", value)
    return body


def _tidy(text: str) -> str:
    """Close up the space that inline markup leaves before punctuation.

    PCO wraps phrases in `insertwords`/`emphasis`/`extref`, and flattening an element
    boundary inserts a space — so the source's "income:" comes out "income :" and an
    amendment note reads "inserted , on 1 April 2008 ,". Cosmetic in isolation, but it
    breaks phrase search and looks like a transcription error in quoted text."""
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)


def _norm(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    # PCO uses NBSP inside dates and act numbers ("1&#160;April"); fold it so the
    # regexes above see ordinary spaces.
    return _tidy(" ".join(" ".join(elem.itertext()).replace("\xa0", " ").split()))


def amendment_relations(root: ET.Element) -> tuple[list[TypedRelation], dict]:
    """Recover the ``history-note`` apparatus as ``AMENDED_BY`` edges.

    A PCO amendment note is already structured — it names the provision amended, the
    operation, the date, and the amending provision and act::

        <history-note><amended-provision>Section A 2(1B) heading</amended-provision>:
        <amending-operation>inserted</amending-operation>, on
        <amendment-date>1 April 2008</amendment-date>, by
        <amending-provision href="DLM1172356">section 307</amending-provision> of the
        <amending-leg>Taxation (Business Taxation and Remedial Matters) Act 2007</amending-leg>
        (2007 No 109)</history-note>

    so throwing it away to clean up the body text would discard a real amendment graph.
    Across the Income Tax Act, 14,307/14,308 notes carry an ``amending-leg`` and 99.8%
    close with "(YYYY No N)" — regular enough to key on.

    That trailing "(2007 No 109)" is the whole reason these become *edges* rather than a
    metadata blob: it maps straight onto the PCO work-id grammar, so the amending act
    gets a real ``nz/act/public/2007/109`` candidate id and the note becomes a live graph
    edge once that act is harvested.

    The **subtype is guessed as ``public``**, which is right for the overwhelming
    majority but wrong for local and private acts (a handful — "New Plymouth District
    Council (Waitara Lands) Act 2018"). That guess is safe here and must stay that way:
    ``dst_id`` is only ever a *candidate*, and ``resolve_pending`` flips an edge live
    only when the id actually exists in the corpus, so a bad guess stays pending forever
    instead of minting a phantom edge.
    """
    relations: list[TypedRelation] = []
    operations: dict[str, int] = {}
    retrospective = 0
    for note in root.iter():
        if localname(note.tag).lower() != "history-note":
            continue
        text = _norm(note)
        if not text:
            continue
        provision = _norm(note.find("amended-provision"))
        operation = _norm(note.find("amending-operation"))
        amending = note.find("amending-provision")
        leg = note.find("amending-leg")
        if operation:
            operations[operation] = operations.get(operation, 0) + 1
        if _RETROSPECTIVE_RE.search(text):
            retrospective += 1

        m = _AMENDING_ACT_RE.search(text)
        dst_id = nz_id("act", "public", m.group(1), m.group(2)) if m else None
        relations.append(TypedRelation(
            relationship_type=RelationshipType.AMENDED_BY,
            # The note verbatim: it is the human-readable annotation, and carries the
            # operation, date and any retrospective effect that the typed fields below
            # have no room for.
            raw_citation_string=text,
            dst_id=dst_id,
            # Which provision of *this* act was changed, so the annotation can be shown
            # against the section a reader is looking at.
            src_anchor=provision or None,
            dst_anchor=_norm(amending) or (_norm(leg) if leg is not None else None),
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    summary = {
        "amendment_notes": len(relations) or None,
        "amendment_operations": operations or None,
        "retrospective_amendments": retrospective or None,
    }
    return relations, summary


def _prune(elem: ET.Element) -> None:
    """Drop non-operative subtrees in place, depth-first."""
    for child in list(elem):
        if localname(child.tag).lower() in _NON_OPERATIVE:
            elem.remove(child)
        else:
            _prune(child)


def _child_by(elem: ET.Element, names: set[str]) -> ET.Element | None:
    return next((c for c in elem if localname(c.tag).lower() in names), None)


def _text(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    return " ".join(" ".join(elem.itertext()).split()) or None


def _classify(elem: ET.Element) -> str:
    """``unit`` | ``container`` | ``pass`` — inferred from shape, hinted by name.

    The decisive test is whether the element **contains other structural elements**, not
    whether it is numbered: Parts carry a ``<label>`` just as provisions do, so keying on
    numbering alone makes a Part look like a giant provision and silently swallows every
    section inside it. Sub-units that belong to a provision's own flow (``subprov``,
    ``para``) are excluded from that test, so a section keeps its subsections as text
    rather than exploding into one segment per paragraph.
    """
    name = localname(elem.tag).lower()
    if name in _PASS_HINTS or name in _HEADINGS or name in _LABELS:
        # Label/heading elements are consumed by their parent's label, never emitted.
        return "pass"
    has_label = _child_by(elem, _LABELS) is not None
    has_heading = _child_by(elem, _HEADINGS) is not None
    has_structural_children = any(
        localname(c.tag).lower() in (_UNIT_HINTS | _CONTAINER_HINTS)
        and localname(c.tag).lower() not in _LINES
        for c in elem)
    if has_structural_children and (has_label or has_heading or name in _CONTAINER_HINTS):
        return "container"
    if has_label or name in _UNIT_HINTS:
        return "unit"
    if has_heading or name in _CONTAINER_HINTS:
        return "container"
    return "pass"


def _label(elem: ET.Element) -> str:
    num = (_text(_child_by(elem, _LABELS)) or "").strip(". ")
    heading = _text(_child_by(elem, _HEADINGS)) or ""
    name = localname(elem.tag).lower()
    prefix = {"part": "Part", "subpart": "Subpart", "schedule": "Schedule",
              "sched": "Schedule", "clause": "cl", "article": "Article"}.get(name, "")
    if not prefix and num and name in _UNIT_HINTS:
        prefix = "s."
    if prefix and num.lower().startswith(prefix.lower().rstrip(".")):
        prefix = ""
    stem = " ".join(p for p in (prefix, num) if p).strip()
    if stem and heading:
        return f"{stem} — {heading}"
    return stem or heading or name


@dataclass(slots=True)
class _Block:
    label: str
    kind: str
    text: str
    level: int


def _walk(elem: ET.Element, level: int, blocks: list[_Block]) -> None:
    for child in elem:
        role = _classify(child)
        name = localname(child.tag).lower()
        if role == "unit":
            text = _tidy(flow_text(child, skip_tags=_LABELS | _HEADINGS, line_tags=_LINES))
            if text.strip():
                blocks.append(_Block(_label(child), "section", text, level))
        elif role == "container":
            header = _label(child)
            kind = "schedule" if name in ("schedule", "sched") else "heading"
            if header:
                blocks.append(_Block(header, kind, header, level))
            before = len(blocks)
            _walk(child, level + 1, blocks)
            if len(blocks) == before:
                # A container whose body is loose prose rather than numbered provisions —
                # emit its text so nothing is dropped on the floor.
                text = _tidy(flow_text(child, skip_tags=_HEADINGS, line_tags=_LINES))
                if text.strip():
                    blocks.append(_Block(header or name, "section", text, level + 1))
        else:
            _walk(child, level, blocks)


def parse_nz_pco_xml(data: bytes) -> ParsedDoc:
    # PCO peppers the text with zero-width no-break spaces inside cross-references
    # ("CW 42(1)﻿(b)"), which would otherwise survive into the indexed text and stop
    # a search for "CW 42(1)(b)" matching.
    source = data.decode("utf-8", errors="replace").replace("﻿", "")
    try:
        root = ET.fromstring(expand_entities(source))
    except ET.ParseError:
        return ParsedDoc()

    # Recover the amendment apparatus *before* pruning it out of the body.
    relations, amendments = amendment_relations(root)
    _prune(root)

    blocks: list[_Block] = []
    _walk(root, 0, blocks)
    inferred = bool(blocks)
    if not blocks:
        # Structure unrecognised — keep the full text. Losing structure is recoverable
        # (re-parse later); losing the document is not.
        body = " ".join("".join(root.itertext()).split())
        if body:
            blocks = [_Block("document", "section", body, 0)]

    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        if parts:
            cursor += len(SEP)
        segments.append(Segment(label=block.label, char_start=cursor,
                                char_end=cursor + len(text), kind=block.kind,
                                level=block.level))
        parts.append(text)
        cursor += len(text)

    title = None
    for e in root.iter():
        if localname(e.tag).lower() in ("title", "shorttitle", "title.act"):
            title = _text(e)
            if title:
                break

    as_at = None
    for attr in ("as.at", "as-at", "date.as.at", "version-date", "date"):
        value = root.get(attr)
        m = _ISO_DATE.search(value or "")
        if m:
            as_at = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            break

    return ParsedDoc(
        text=SEP.join(parts) or None,
        segments=segments,
        relations=relations,
        title=title,
        decision_date=as_at,
        metadata={
            "root": localname(root.tag).lower(),
            "as_at": as_at,
            # Whether shape-inference found structure, or the whole-text fallback ran.
            "inferred_structure": inferred,
            # The PCO's own document id. Every provision carries one too, and the
            # cross-reference markup (`extref href="DLM245345"`) addresses them — so
            # recording it builds a DLM→stable_id index across the corpus as it
            # harvests, which is what a later cross-reference pass needs.
            "dlm_id": root.get("id"),
            "act_no": root.get("act.no"),
            "act_type": root.get("act.type"),
            "date_assent": root.get("date.assent"),
            "date_first_valid": root.get("date.first.valid"),
            # The `end.reprint-note` "amendments incorporated" list is deliberately NOT
            # stored: it runs to ~10KB on a large act (170MB of meta_json across the
            # 17k-act corpus) and is the prose form of the amendment edges above.
            **{k: v for k, v in amendments.items() if v is not None},
        },
    )


register("nz-pco-xml", parse_nz_pco_xml)
