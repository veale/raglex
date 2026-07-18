"""Hong Kong e-Legislation parser — the HKLM schema (``xml.gov.hk/schemas/hklm/1.0``).

Hong Kong publishes its consolidated laws as a bulk XML corpus in a purpose-built
schema. Two features shape this parser:

1. **``<sourceNote>`` is amendment provenance interleaved with the law.** Every provision
   trails notes like ``(Amended L.N. 130 of 2007)`` or ``(Format changes—E.R. 1 of
   2013)``. Left inline they read as part of the operative text and corrupt any diff
   between consolidations, so they are lifted out and returned as structured
   ``metadata["source_notes"]`` — the same treatment given Canadian ``HistoricalNote``
   chains ([[australian-legislation]]-era idiom) and Irish LRC annotations.

2. **``<ref href>`` mixes corpus targets with non-corpus ones**, and conflating them is
   the hanging-reference trap:

   * ``/hk/cap486``, ``/hk/cap4A``, ``/hk/cap486/s2`` — an Ordinance, a piece of
     subsidiary legislation, or a pinpoint into one. These **are** corpus nodes, so they
     get a resolved ``dst_id``.
   * ``/hk/A206`` — a constitutional instrument (the Basic Law and its companions), also
     a corpus node.
   * ``/hk/1996/ln343`` (Legal Notice), ``/hk/2013/er1`` (Editorial Record),
     ``/hk/2012/18`` (Ordinance 18 of 2012) — these name the *amending instruments*,
     which the consolidated corpus does not carry as separate documents. They are
     recorded as citation strings with **no** ``dst_id`` rather than minted as ids that
     could never resolve.

Identity is the **chapter number**, which is how Hong Kong law is cited and how the
schema's own cross-references address their targets: ``Cap. 486`` → ``hk/cap/486``,
``Cap. 4 sub. leg. A`` → ``hk/cap/4a``.
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

HKLM_NS = "http://www.xml.gov.hk/schemas/hklm/1.0"

# The label children of a provision — they become the segment label, and sourceNote is
# removed from the tree entirely (below), so none may repeat in the body text.
_SKIP = {"num", "heading", "sourcenote"}
# Sub-units that start a new line, so enumerated provisions read as a list.
_LINES = {"subsection", "paragraph", "subparagraph", "def", "leadin", "continued", "item"}

# Containers that emit a heading block, then recurse.
_CONTAINERS = {"part", "division", "subdivision", "crossheading", "schedule", "annex"}
# Leaf provisions — one segment each.
_UNITS = {"section", "longtitle", "preamble", "rule", "article"}
# Descended through silently.
_PASS = {"ordinance", "subleg", "resolution", "lawdoc", "main", "body", "content",
         "group", "schedules"}

# /hk/cap486  ·  /hk/cap4A  ·  /hk/cap486/s2  ·  /hk/cap132CI/s5
_CAP_REF = re.compile(r"^/?hk/cap(?P<cap>[A-Z]?\d+[A-Z]*)(?:/s(?P<sec>[\w.()]+))?$", re.I)
# /hk/A206 — a constitutional instrument
_INSTRUMENT_REF = re.compile(r"^/?hk/(?P<id>A\d+[A-Z]*)$", re.I)
# dc:identifier — /hk/cap486!en
_IDENTIFIER = re.compile(r"^/?hk/(?P<key>[^!]+)(?:!(?P<lang>\w+))?$", re.I)


def hk_id(kind: str, number: str, lang: str = "en") -> str:
    """The corpus stable_id for a Hong Kong instrument: ``hk/cap/486``, ``hk/cap/132ci``,
    ``hk/instrument/a206``.

    The chapter number carries the subsidiary-legislation suffix as part of itself
    (``Cap. 4 sub. leg. A`` is ``4A``), because that is how the schema's own
    cross-references address it — keeping the register's key rather than inventing a
    parent/child pair means a ``<ref href="/hk/cap4A">`` resolves by construction.
    """
    slug = re.sub(r"[^a-z0-9]", "", (number or "").lower())
    base = f"hk/{kind}/{slug}"
    return base if (lang or "en").lower() in ("", "en") else f"{base}/{lang.lower()}"


# A constitutional instrument's number: A101, A206, A305B. The register addresses these
# BOTH ways — its own dc:identifier says "capA101" while cross-references say "/hk/A206" —
# so both forms must fold to one id or the Basic Law's companions never link up.
_INSTRUMENT_NUMBER = re.compile(r"^A\d+[A-Z]*$", re.I)


def _identity(identifier: str | None, doc_number: str | None,
              doc_type: str) -> tuple[str, str]:
    """``(kind, number)`` from the register's own key, preferring ``dc:identifier``
    (which already carries the subsidiary-legislation suffix, e.g. ``capA101``/``cap4A``)
    and falling back to ``docNumber``."""
    key = None
    m = _IDENTIFIER.match(identifier or "")
    if m:
        key = m.group("key")
        if key.lower().startswith("cap"):
            key = key[3:]
    if not key:
        key = doc_number or ""
    if _INSTRUMENT_NUMBER.match(key) or doc_type == "instrument":
        return "instrument", key
    return "cap", key


# The commencement formula every Ordinance opens with. HKLM has no short-title element —
# the short title is stated in section 1 as running text ("This Ordinance may be cited as
# the Personal Data (Privacy) Ordinance") — so without this the corpus would be titled
# "Cap. 486", which is not how anyone searches for or cites it.
_CITED_AS = re.compile(
    r"may be cited as the\s+(?P<title>.{3,120}?)\s*(?=[.,;]|\s+and shall|\s+and comes|$)",
    re.I | re.S)


def _short_title(text: str | None) -> str | None:
    m = _CITED_AS.search(text or "")
    if not m:
        return None
    return " ".join(m.group("title").split()).strip(" .,;") or None


def _ref_target(href: str) -> tuple[str | None, str | None]:
    """A ``<ref href>`` → ``(stable_id | None, pinpoint anchor | None)``.

    ``None`` for the id means "a real citation to something outside the consolidated
    corpus" (a Legal Notice, an Editorial Record, an Ordinance-of-year), which is kept
    as a citation string without a destination.
    """
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return None, None
    m = _CAP_REF.match(href)
    if m:
        sec = m.group("sec")
        # "/hk/capA206" and "/hk/A206" name the same constitutional instrument — fold
        # both through the same identity rule so they can't produce two nodes.
        kind, number = _identity(None, m.group("cap"), "")
        return hk_id(kind, number), (f"s. {sec}" if sec else None)
    m = _INSTRUMENT_REF.match(href)
    if m:
        return hk_id("instrument", m.group("id")), None
    return None, None


@dataclass(frozen=True, slots=True)
class SourceNote:
    """One provision's amendment provenance, lifted out of its text —
    "(Amended L.N. 130 of 2007)"."""
    provision: str | None
    text: str
    # the instruments named in the note, as raw citation strings ("L.N. 130 of 2007")
    instruments: tuple[str, ...] = ()


def _text(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    return " ".join(" ".join(elem.itertext()).split()) or None


def _child(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    return next((c for c in elem if localname(c.tag).lower() == name.lower()), None)


def _child_text(elem: ET.Element | None, name: str) -> str | None:
    return _text(_child(elem, name))


def _take_source_notes(unit: ET.Element, provision: str | None) -> list[SourceNote]:
    """Remove ``<sourceNote>`` blocks from a provision and record what they cite.

    Materialised before iterating because the loop mutates the tree.
    """
    out: list[SourceNote] = []
    for parent in list(unit.iter()):
        for block in [c for c in parent if localname(c.tag).lower() == "sourcenote"]:
            body = _text(block)
            if body:
                instruments = tuple(
                    t for t in (_text(r) for r in block.iter()
                                if localname(r.tag).lower() == "ref") if t)
                out.append(SourceNote(provision=provision, text=body,
                                      instruments=instruments))
            parent.remove(block)
    return out


def _collect_refs(unit: ET.Element, src_label: str, relations: list[TypedRelation]) -> None:
    for e in unit.iter():
        if localname(e.tag).lower() != "ref":
            continue
        href = e.get("href") or ""
        dst_id, anchor = _ref_target(href)
        if dst_id is None:
            continue   # a real citation, but not to a document the corpus holds
        relations.append(TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string=_text(e) or href,
            dst_id=dst_id, src_anchor=src_label, dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))


def _label(elem: ET.Element, kind: str) -> str:
    """A provision's citable label: "s. 4 — Data protection principles"."""
    num = (_child_text(elem, "num") or "").strip(". ")
    heading = _child_text(elem, "heading") or ""
    prefix = {"section": "s.", "part": "Part", "division": "Division",
              "schedule": "Schedule", "article": "Article", "rule": "r."}.get(kind, "")
    # Containers often spell their own name in the num ("Part IV"); don't double it.
    if prefix and num.lower().startswith(prefix.lower().rstrip(".")):
        prefix = ""
    stem = " ".join(p for p in (prefix, num) if p).strip() or kind
    return f"{stem} — {heading}" if heading else stem


@dataclass(slots=True)
class _Block:
    label: str
    kind: str
    text: str
    level: int


def _walk(elem: ET.Element, level: int, blocks: list[_Block], notes: list[SourceNote],
          relations: list[TypedRelation]) -> None:
    for child in elem:
        name = localname(child.tag).lower()
        if name in _UNITS:
            label = _label(child, name)
            notes.extend(_take_source_notes(child, label))
            _collect_refs(child, label, relations)
            text = flow_text(child, skip_tags=_SKIP, line_tags=_LINES)
            if text.strip():
                blocks.append(_Block(label, "section", text, level))
        elif name in _CONTAINERS:
            header = _label(child, name)
            notes.extend(_take_source_notes(child, header))
            if header:
                blocks.append(_Block(header, "schedule" if name == "schedule" else name,
                                     header, level))
            before = len(blocks)
            _walk(child, level + 1, blocks, notes, relations)
            if len(blocks) == before:
                # A container whose body is loose prose/tables rather than numbered
                # provisions — the Basic Law's annexes are the main case. Without this
                # its entire text would be silently dropped on the floor, since there is
                # no <section> inside for the unit branch to pick up.
                _collect_refs(child, header, relations)
                text = flow_text(child, skip_tags=_SKIP, line_tags=_LINES)
                if text.strip():
                    blocks.append(_Block(header or name, "section", text, level + 1))
        elif name in _PASS:
            _walk(child, level, blocks, notes, relations)


def _dedupe(relations: list[TypedRelation]) -> list[TypedRelation]:
    seen: set[tuple] = set()
    out = []
    for r in relations:
        key = (r.dst_id, r.dst_anchor, r.src_anchor)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _iso(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "")[:10])
    except ValueError:
        return None


def parse_hklm_xml(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()

    doc_kind = localname(root.tag).lower()      # ordinance | subLeg | resolution | lawDoc
    meta_el = _child(root, "meta")
    lang = (_child_text(meta_el, "language")
            or root.get("{http://www.w3.org/XML/1998/namespace}lang") or "en").lower()

    doc_name = _child_text(meta_el, "docName")       # "Cap. 486"
    doc_number = _child_text(meta_el, "docNumber")   # "486"
    doc_type = (_child_text(meta_el, "docType") or "").lower()   # cap | instrument
    doc_status = _child_text(meta_el, "docStatus")   # In effect | Repealed | …
    identifier = _child_text(meta_el, "identifier")  # "/hk/cap486!en"
    version_date = _iso(_child_text(meta_el, "date"))

    # Identity comes from dc:identifier where present (it is the register's own key and
    # already carries the subsidiary-legislation suffix), else from docNumber.
    kind, number = _identity(identifier, doc_number, doc_type)

    blocks: list[_Block] = []
    notes: list[SourceNote] = []
    relations: list[TypedRelation] = []
    _walk(root, 0, blocks, notes, relations)
    if not blocks:  # unrecognised shape — keep the text rather than losing the document
        body = " ".join("".join(root.itertext()).split())
        if body:
            blocks = [_Block(doc_name or "document", "section", body, 0)]

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

    # The long title doubles as the document's descriptive title where there's no
    # short title element (the schema puts the short title inside <docTitle>).
    doc_title = None
    for e in root.iter():
        if localname(e.tag).lower() == "doctitle":
            doc_title = _text(e)
            break
    # Ordinances carry no <docTitle>; their short title is stated in section 1.
    short_title = doc_title or _short_title(SEP.join(parts[:4]))

    return ParsedDoc(
        text=SEP.join(parts) or None,
        segments=segments,
        relations=_dedupe(relations),
        title=short_title or doc_name,
        decision_date=version_date,
        metadata={
            "kind": kind,
            "number": number,
            "doc_name": doc_name,
            "short_title": short_title,
            "doc_type": doc_type,
            "doc_kind": doc_kind,
            "doc_status": doc_status,
            "identifier": identifier,
            "language": lang,
            "version_date": version_date,
            "repealed": (doc_status or "").lower().startswith("repealed"),
            "in_effect": (doc_status or "").lower() == "in effect",
            "source_notes": notes,
        },
    )


register("hklm-xml", parse_hklm_xml)
