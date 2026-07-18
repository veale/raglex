"""Justice Canada XML parser — the federal ``Statute`` / ``Regulation`` schema.

Canada's Justice Laws corpus is the cleanest legislative XML in the series, and its
distinguishing feature is the **``lims:`` namespace** (``http://justice.gc.ca/lims``),
which carries first-class temporal and identity metadata *on every provision*, not just
on the document. That changes what a parser is for: this is not prose to flatten, it is
a structured point-in-time record to preserve.

Three things this parser does that the prose-oriented parsers don't:

1. **Provision-level temporal capture.** Every ``Section``/``Subsection`` may carry
   ``lims:inforce-start-date``, ``lims:lastAmendedDate`` and ``lims:enacted-date``. These
   are hoisted onto the emitted segments' metadata (``metadata["provisions"]``) so a
   caller can reconstruct "what was in force on date D" at *section* granularity from a
   single consolidated file — the finest-grained point-in-time available anywhere in the
   corpus. The document-level ``lims:pit-date`` stamps the snapshot as a whole.

2. **``HistoricalNote`` is lifted out of the operative text.** Each provision trails its
   own amendment provenance ("R.S., 1985, c. A-1, s. 3; 1992, c. 21, s. 1; 2002, c. 8,
   s. 183"). Left inline it pollutes the law with citation debris and wrecks diffs
   between consolidations; so it is removed from the text and returned as structured
   ``metadata["historical_notes"]``, which the adapter turns into ``amended_by`` edges —
   the same treatment the Irish parser gives LRC annotations ([[irish-legislation]]).

3. **``XRefExternal`` is a machine-linked edge, not a citation string.** The schema names
   its target by *code* (``link="F-27"``, ``reference-type="act"``), so cross-references
   resolve without any citation grammar at all. The regulation→enabling-Act link in
   ``Identification/EnablingAuthority`` is the same mechanism and is the single most
   valuable edge in the Canadian corpus (Australia's ``based_on`` analogue).

``Repealed`` sections are kept, flagged rather than dropped: repealed law stays
addressable so case-law citations to it still resolve.
"""

from __future__ import annotations

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

LIMS = "http://justice.gc.ca/lims"


def _lims(elem: ET.Element, name: str) -> str | None:
    return elem.get(f"{{{LIMS}}}{name}")


# The label children of a provision — they become the segment label, so they must not be
# repeated in the body text. HistoricalNote is removed from the tree entirely (below).
_SKIP = {"label", "marginalnote", "historicalnote"}
# Sub-units that start a new line, so enumerated provisions read as a list.
_LINES = {"subsection", "paragraph", "subparagraph", "clause", "definition", "provision"}

# Structural containers we descend through without emitting a block of their own.
_PASS = {"statute", "regulation", "body", "introduction", "order", "list", "formgroup",
         "bilingualgroup", "documentinternal", "billpiece", "regulationpiece"}
# Containers that DO emit a heading block (their TitleText) before their children.
_CONTAINERS = {"heading", "scheduleformheading"}
# Leaf provisions — one segment each.
_UNITS = {"section", "provision", "item"}

_REF_TYPES = {"act", "regulation"}


def ca_id(kind: str, code: str, lang: str = "eng") -> str:
    """The corpus stable_id for a federal instrument: ``ca/act/A-1``, ``ca/regulation/
    sor-2018-69``.

    Canada keys Acts by their **consolidated chapter code** (``A-1``, ``C-46``) and
    regulations by **instrument number** (``C.R.C., c. 870``, ``SOR/2018-69``) — these
    are the register's own permanent identifiers and the target of every
    ``XRefExternal link=``, so they are kept verbatim rather than re-invented, only
    lowercased and path-safed.

    Language is part of the id because English and French are **equally authoritative**
    (not translations) — each is a distinct, separately addressable Expression of the
    same Work. ``eng`` is unsuffixed so the English id stays the stable primary key and
    turning French on later adds nodes instead of renaming them.

    The same instrument is named three ways across the corpus — as an ``InstrumentNumber``
    (``C.R.C., c. 870``), as an ``XRefExternal link`` (``C.R.C.,_c._870``) and as a
    filename stem (``C.R.C.,_c._870.xml``) — so all three must fold to one id or every
    cross-reference dangles. Acts and regulations fold differently **on purpose**:
    regulation punctuation is noise (``C.R.C.`` is an abbreviation), but an Act code's
    dot is significant — ``A-1.3`` and ``A-13`` are different Acts, so stripping dots
    from Act codes would silently merge them.
    """
    slug = (code or "").strip().lower()
    if kind == "act":
        slug = slug.replace(" ", "").replace("_", "-")
    else:
        slug = slug.replace(".", "")
        for sep in ("_", "/", ",", " "):
            slug = slug.replace(sep, "-")
        while "--" in slug:
            slug = slug.replace("--", "-")
    slug = slug.strip("-.")
    base = f"ca/{kind}/{slug}"
    return base if (lang or "eng").lower() in ("", "eng", "en") else f"{base}/{lang.lower()}"


def _child(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    return next((c for c in elem if localname(c.tag).lower() == name.lower()), None)


def _text(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    return " ".join(" ".join(elem.itertext()).split()) or None


def _child_text(elem: ET.Element | None, name: str) -> str | None:
    return _text(_child(elem, name))


def _date(elem: ET.Element | None) -> date | None:
    """The schema spells dates out as ``<Date><YYYY/><MM/><DD/></Date>`` rather than as
    an ISO string, so it needs assembling (unlike the ``lims:`` attributes, which are
    already ``YYYY-MM-DD``)."""
    if elem is None:
        return None
    # `or` would test the element's truth value, which ElementTree deprecates (an element
    # with no children is falsey) — an explicit None check is the correct form.
    node = _child(elem, "date")
    if node is None:
        node = elem
    try:
        return date(int(_child_text(node, "yyyy") or 0),
                    int(_child_text(node, "mm") or 0),
                    int(_child_text(node, "dd") or 0))
    except (TypeError, ValueError):
        return None


def _iso(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "")[:10])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class HistoricalNote:
    """One provision's amendment provenance, lifted out of its text.

    ``citation`` is the raw chain item as published ("2019, c. 18, s. 2"); the adapter
    parses it into an amending-instrument edge. ``original`` marks the enacting entry
    (``type="original"``) as distinct from later amendments.
    """
    provision: str | None
    citation: str
    original: bool = False
    inforce_start: date | None = None


@dataclass(frozen=True, slots=True)
class Provision:
    """A provision's ``lims:`` temporal record — the point-in-time payload."""
    label: str
    inforce_start: date | None = None
    last_amended: date | None = None
    enacted: date | None = None
    repealed: bool = False


def _take_historical_notes(unit: ET.Element, provision: str | None) -> list[HistoricalNote]:
    """Remove ``<HistoricalNote>`` blocks from a provision and parse their sub-items.

    Materialised before iterating because the loop mutates the tree; each
    ``HistoricalNoteSubItem`` is one link in the provision's amendment chain.
    """
    out: list[HistoricalNote] = []
    for parent in list(unit.iter()):
        for block in [c for c in parent if localname(c.tag).lower() == "historicalnote"]:
            for item in block.iter():
                if localname(item.tag).lower() != "historicalnotesubitem":
                    continue
                citation = _text(item)
                if citation:
                    out.append(HistoricalNote(
                        provision=provision,
                        citation=citation,
                        original=(item.get("type") == "original"),
                        inforce_start=_iso(_lims(item, "inforce-start-date")),
                    ))
            parent.remove(block)
    return out


def _collect_xrefs(unit: ET.Element, src_label: str, relations: list[TypedRelation]) -> None:
    """Turn ``<XRefExternal reference-type="act" link="F-27">`` into edges.

    Only ``act``/``regulation`` targets are minted: ``other``, ``standard`` and
    ``canada-gazette`` reference-types point outside the legislative corpus and would
    mint edges that can never resolve — the hanging-reference trap.
    """
    for e in unit.iter():
        if localname(e.tag).lower() != "xrefexternal":
            continue
        link = (e.get("link") or "").strip()
        ref_type = (e.get("reference-type") or "").lower()
        if not link or ref_type not in _REF_TYPES:
            continue
        relations.append(TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string=_text(e) or link,
            dst_id=ca_id("act" if ref_type == "act" else "regulation", link),
            src_anchor=src_label,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))


def _label(elem: ET.Element, kind: str) -> str:
    """A provision's citable label: its ``<Label>`` number plus its ``<MarginalNote>``
    heading ("s. 3 — Definitions"), which is how Justice Laws itself presents it."""
    num = (_child_text(elem, "label") or "").strip(". ")
    note = _child_text(elem, "marginalnote") or ""
    if kind == "section" and num:
        stem = f"s. {num}"
    elif num:
        stem = num
    else:
        stem = kind
    return f"{stem} — {note}" if note else stem


@dataclass(slots=True)
class _Block:
    label: str
    kind: str
    text: str
    level: int


def _walk(elem: ET.Element, level: int, blocks: list[_Block], notes: list[HistoricalNote],
          provisions: list[Provision], relations: list[TypedRelation]) -> None:
    for child in elem:
        name = localname(child.tag).lower()
        if name in _UNITS:
            label = _label(child, name)
            notes.extend(_take_historical_notes(child, label))
            _collect_xrefs(child, label, relations)
            repealed = _child(child, "repealed") is not None
            provisions.append(Provision(
                label=label,
                inforce_start=_iso(_lims(child, "inforce-start-date")),
                last_amended=_iso(_lims(child, "lastAmendedDate")),
                enacted=_iso(_lims(child, "enacted-date")),
                repealed=repealed,
            ))
            text = flow_text(child, skip_tags=_SKIP, line_tags=_LINES)
            if text.strip():
                blocks.append(_Block(label, "section", text, level))
        elif name in _CONTAINERS:
            header = _child_text(child, "titletext") or _text(child)
            if header:
                blocks.append(_Block(header, "heading", header,
                                     max(0, int(child.get("level") or level or 1) - 1)))
            _walk(child, level + 1, blocks, notes, provisions, relations)
        elif name == "schedule":
            header = (_child_text(child, "scheduleformheading")
                      or _child_text(child, "titletext") or "Schedule")
            blocks.append(_Block(header, "schedule", header, level))
            _walk(child, level + 1, blocks, notes, provisions, relations)
        elif name in _PASS:
            _walk(child, level, blocks, notes, provisions, relations)


def _dedupe(relations: list[TypedRelation]) -> list[TypedRelation]:
    seen: set[tuple] = set()
    out = []
    for r in relations:
        key = (r.dst_id, r.src_anchor)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def parse_lims_xml(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()

    kind = localname(root.tag).lower()          # statute | regulation
    ident = _child(root, "identification")
    lang = (root.get("{http://www.w3.org/XML/1998/namespace}lang") or "eng").lower()
    lang = {"en": "eng", "fr": "fra"}.get(lang, lang)

    short = _child_text(ident, "shorttitle")
    long_title = _child_text(ident, "longtitle")

    # Identity: Acts carry a consolidated chapter code, regulations an instrument number.
    chapter = _child_text(_child(ident, "chapter"), "consolidatednumber")
    instrument = _child_text(ident, "instrumentnumber")
    code = chapter or instrument

    # The regulation→enabling-Act edge, machine-linked by code — the corpus's best edge.
    relations: list[TypedRelation] = []
    enabling = _child(ident, "enablingauthority")
    enabling_id = None
    if enabling is not None:
        xref = _child(enabling, "xrefexternal")
        link = (xref.get("link") or "").strip() if xref is not None else ""
        if link:
            enabling_id = ca_id("act", link, lang)
            # Typed IMPLEMENTS + "made under" to match the Irish enabling-power edge
            # ([[irish-legislation]]) rather than minting a Canada-only edge type.
            relations.append(TypedRelation(
                relationship_type=RelationshipType.IMPLEMENTS,
                raw_citation_string=_text(xref) or link,
                dst_id=enabling_id,
                dst_anchor="made under",
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))

    blocks: list[_Block] = []
    notes: list[HistoricalNote] = []
    provisions: list[Provision] = []
    _walk(root, 0, blocks, notes, provisions, relations)
    if not blocks:  # unrecognised shape — keep the text rather than losing the document
        body = " ".join("".join(root.itertext()).split())
        if body:
            blocks = [_Block(short or code or "document", "section", body, 0)]

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

    consolidated = _date(_child(ident, "consolidationdate")) or _date(
        _child(_child(_child(ident, "billhistory"), "stages"), "date"))

    return ParsedDoc(
        text=SEP.join(parts) or None,
        segments=segments,
        relations=_dedupe(relations),
        title=short or long_title,
        # The point-in-time this file represents — the whole reason to hold the file.
        decision_date=_iso(_lims(root, "pit-date")),
        metadata={
            "kind": "regulation" if kind == "regulation" else "act",
            "code": code,
            "chapter": chapter,
            "instrument_number": instrument,
            "long_title": long_title,
            "short_title": short,
            "language": lang,
            "regulation_type": root.get("regulation-type"),
            "bill_origin": root.get("bill-origin"),
            "bill_type": root.get("bill-type"),
            "in_force": root.get("in-force"),
            "has_previous_version": root.get("hasPreviousVersion") == "true",
            "pit_date": _iso(_lims(root, "pit-date")),
            "last_amended_date": _iso(_lims(root, "lastAmendedDate")),
            "current_date": _iso(_lims(root, "current-date")),
            "inforce_start_date": _iso(_lims(root, "inforce-start-date")),
            "consolidation_date": consolidated,
            "lims_id": _lims(root, "id"),
            "enabling_act_id": enabling_id,
            "repealed": _child(root, "repealed") is not None,
            "historical_notes": notes,
            "provisions": provisions,
        },
    )


register("lims-xml", parse_lims_xml)
