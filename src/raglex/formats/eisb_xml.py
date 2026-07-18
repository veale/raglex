"""Irish Statute Book XML parser — the eISB *as-enacted* and LRC *revised* schemas.

Ireland publishes two custom XML schemas, not Akoma Ntoso, and both root at ``<act>``
with the same ``<metadata><title/><number/><year/><dateofenactment/></metadata>`` head.
They diverge in three ways that this parser reconciles so callers see one shape:

1. **Presentation glyphs are elements, not characters.** The text carries ``<emdash/>``,
   ``<odq/>``/``<cdq/>`` (curly quotes), ``<csq/>`` (the apostrophe in "State's"),
   ``<euro/>``, ``<afada/>``/``<Efada/>`` (the Irish acute-accented vowels), and
   ``<unicode ch="00E0"/>``. Dropping them silently corrupts the text — "State s",
   "Minister" quotes lost, Irish names mangled — so they are substituted back to
   characters *before* the tree is built. A few older files also escape ``\\u2014``
   literally in the source; those are decoded too.

2. **The LRC revised XML declares DTD entities** (``&updatedtodate;``, ``&lastact;``,
   ``&lastsi;``) that a DTD-less parser rejects outright. The values are right there in
   the document's own **internal DTD subset**, so we read them from the bytes and
   substitute — no network fetch of ``legislation.dtd``, no external-entity resolution,
   no XXE surface (§Part 7.2 of the manual proposes scraping the values off the HTML
   page; reading the internal subset is both safer and exact).

3. **The revised text interleaves annotations** — ``<div class="annotations">`` blocks
   holding the LRC's F-notes (textual amendments), C-notes (non-textual effects) and
   E-notes (editorial/commencement). They are lifted *out* of the provision text (so the
   operative law reads clean and diffs against the as-enacted version) and returned as
   structured records in ``metadata["annotations"]``, each parsed for the effect, the
   operative date, and the affecting instrument — which is what the adapter turns into
   amendment edges.

Segments follow the document's own hierarchy: Part → Chapter → section, plus Schedules
from the backmatter, so a retrieval hit maps back to a citable provision.
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

# Empty elements that stand for a character. Anything not listed is dropped, which is
# right for the true presentation elements (<hr1/>, <graphic/>, <page/>).
_GLYPHS = {
    "emdash": "—", "endash": "–", "hyphen": "-", "eolhyphen": "",
    "odq": "“", "cdq": "”", "osq": "‘", "csq": "’",
    "bull": "•", "euro": "€", "pound": "£", "nbsp": " ",
    "degree": "°", "half": "½", "quarter": "¼",
}
# á é í ó ú and capitals — written as <afada/>, <Efada/>, … (fada = the acute accent).
_GLYPHS.update({f"{v}fada": a for v, a in zip("aeiouAEIOU", "áéíóúÁÉÍÓÚ")})

_GLYPH_RE = re.compile(r"<(" + "|".join(_GLYPHS) + r")\s*/>")
_UNICODE_RE = re.compile(r'<unicode\s+ch="([0-9A-Fa-f]{4})"\s*/>')
_ESCAPED_RE = re.compile(r"\\u([0-9A-Fa-f]{4})")
_DOCTYPE_RE = re.compile(r"<!DOCTYPE.*?(?:\[.*?\])?\s*>", re.S)
_ENTITY_DECL_RE = re.compile(r'<!ENTITY\s+(?P<name>[A-Za-z_][\w.-]*)\s+"(?P<value>[^"]*)"\s*>')

# The document's own label children — they become the segment label, so they must not
# also be repeated in the body text.
_SKIP = {"number", "title"}
# Sub-units that each start a new line, so enumerated provisions read as a list.
_LINES = {"p", "pc"}

_CONTAINERS = {"part", "chapter", "subpart"}
_UNITS = {"sect", "section", "schedule", "article", "rule"}
_PASS = {"act", "body", "mainbody", "backmatter", "group"}

# External cross-reference targets, in the two forms the schemas use:
#   EN.ACT.2003.0032[#SEC5]  — the legacy path key
#   ZZA32Y2003[S26]          — "Act 32 of 2003, section 26"
_XREF_PATH = re.compile(r"^(?P<lang>[A-Z]{2})\.(?P<type>ACT|SI|SRO|PRV)\.(?P<year>\d{4})\.(?P<num>\d+)"
                        r"(?:#(?P<anchor>[A-Za-z0-9]+))?$", re.IGNORECASE)
_XREF_ZZ = re.compile(r"^ZZ(?P<type>A|SI|SRO)(?P<num>\d+[A-Za-z]?)Y(?P<year>\d{4})"
                      r"(?:S(?P<sec>\d+[A-Za-z]?))?$", re.IGNORECASE)
_ZZ_TYPE = {"A": "act", "SI": "si", "SRO": "sro"}


def eli_id(typ: str, year: str | int, number: str, lang: str = "en") -> str:
    """The corpus stable_id for an Irish instrument: ``ie/2003/act/32``.

    The ELI Work path (``{year}/{type}/{number}``) prefixed with the jurisdiction, per
    the manual's "keep the ELI Work URI verbatim, don't invent an id". The number stays
    a **string** — SROs carry ``1a``/``1b`` suffixes and synthetic ``999`` ids, and
    pre-1922 Acts use the chapter number. A non-English expression appends its language
    so the Irish text of a bilingual Act is a distinct, addressable node."""
    num = str(number).strip().lstrip("0") or str(number).strip()
    base = f"ie/{year}/{typ.lower()}/{num.lower()}"
    return base if lang.lower() in ("", "en") else f"{base}/{lang.lower()}"


def _xref_target(href: str) -> tuple[str, str | None] | None:
    """An external xref href → (stable_id, pinpoint anchor). Internal anchors (``#SEC1``)
    return None — they address this document, and minting edges for them buried the
    hanging-reference worklist in the UK adapter for exactly the same reason."""
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return None
    m = _XREF_PATH.match(href)
    if m:
        anchor = (m.group("anchor") or "").upper()
        sec = anchor[3:] if anchor.startswith("SEC") else None
        return eli_id(m.group("type"), m.group("year"), m.group("num")), (f"s. {sec}" if sec else None)
    m = _XREF_ZZ.match(href)
    if m:
        typ = _ZZ_TYPE.get(m.group("type").upper())
        if typ:
            sec = m.group("sec")
            return eli_id(typ, m.group("year"), m.group("num")), (f"s. {sec}" if sec else None)
    return None


def expand_entities(text: str) -> str:
    """Substitute the entities the LRC declares in the document's own internal DTD
    subset, then drop the DOCTYPE. The declared values legitimately contain markup
    (``&lastact;`` expands to an italicised Act title), so this is a text-level
    substitution done before the tree is built — the only way ElementTree, which
    ignores internal subsets and hard-fails on an undefined entity, can read the file
    at all. Nothing external is ever fetched."""
    doctype = _DOCTYPE_RE.search(text)
    if not doctype:
        return text
    entities = {m.group("name"): m.group("value")
                for m in _ENTITY_DECL_RE.finditer(doctype.group(0))}
    body = text[:doctype.start()] + text[doctype.end():]
    for name, value in entities.items():
        body = body.replace(f"&{name};", value)
    return body


_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}
_PROSE_DATE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})")


def prose_date(text: str | None) -> date | None:
    """"1 June 2025" → ``date(2025, 6, 1)``. The LRC states its consolidation point as
    prose, spelled out in the XML ("1 June 2025") but abbreviated in the list page
    ("1 Jun 2025"), so both are accepted. A "latest" pull is undatable — and therefore
    unplaceable in time — unless this date is captured and stamped on the snapshot."""
    m = _PROSE_DATE.search(text or "")
    if not m:
        return None
    name = m.group(2).lower()
    month = _MONTHS.get(name) or next((v for k, v in _MONTHS.items() if k.startswith(name)), None)
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _updated_to(source: str) -> date | None:
    doctype = _DOCTYPE_RE.search(source)
    if not doctype:
        return None
    for m in _ENTITY_DECL_RE.finditer(doctype.group(0)):
        if m.group("name") == "updatedtodate":
            return prose_date(m.group("value"))
    return None


def decode(data: bytes) -> str:
    """Raw bytes → parseable XML text: entities expanded, glyph elements turned back
    into the characters they stand for."""
    text = data.decode("utf-8", errors="replace")
    text = expand_entities(text)
    text = _ESCAPED_RE.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = _UNICODE_RE.sub(lambda m: _escape(chr(int(m.group(1), 16))), text)
    return _GLYPH_RE.sub(lambda m: _escape(_GLYPHS[m.group(1)]), text)


def _escape(ch: str) -> str:
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(ch, ch)


# -- annotations (LRC revised only) -----------------------------------------
# "Substituted (25.05.2018) by Data Protection Act 2018 (7/2018), s. 194, S.I. No. 174
# of 2018, art. 3" — effect, operative date, affecting instrument, its provisions.
# The bracketed date is the date the effect was COMMENCED, not enacted (the LRC states
# this explicitly), so it is stored as an operative date and never as an enactment date.
_NOTE_RE = re.compile(
    r"^(?P<effect>.{0,200}?)"
    r"(?:\((?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\)\s*)?"
    r"by\s+(?P<instrument>.{0,200}?)\s*"
    r"\((?:(?:S\.?I\.?\s*No\.?\s*(?P<si_num>\d+)\s+of\s+(?P<si_year>\d{4}))"
    r"|(?:(?P<act_num>\d+)/(?P<act_year>\d{4})))\)",
    re.IGNORECASE | re.S)


@dataclass(frozen=True, slots=True)
class Annotation:
    """One LRC amendment/effect note, attached to the provision it sits under."""
    note_type: str          # F (textual amendment) | C (non-textual effect) | E (editorial)
    provision: str | None   # the section label the note sits under
    text: str
    effect: str | None = None          # "Substituted", "Application of Act extended", …
    operative_date: date | None = None  # the bracketed date = COMMENCEMENT, not enactment
    affecting_id: str | None = None     # ie/2018/act/7 — the amending instrument
    affecting_title: str | None = None


def parse_annotation(note_type: str, provision: str | None, text: str) -> Annotation:
    """Parse one note's prose into a structured effect. Unparseable prose still yields
    an Annotation (text preserved) — the notes are only semi-regular, and a note we
    can't decompose is still worth showing to a reader."""
    flat = " ".join(text.split())
    m = _NOTE_RE.match(flat)
    if not m:
        return Annotation(note_type=note_type, provision=provision, text=flat)
    when = None
    if m.group("year"):
        try:
            when = date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
        except ValueError:
            when = None
    if m.group("si_num"):
        affecting = eli_id("si", m.group("si_year"), m.group("si_num"))
    else:
        affecting = eli_id("act", m.group("act_year"), m.group("act_num"))
    return Annotation(
        note_type=note_type, provision=provision, text=flat,
        effect=(m.group("effect") or "").strip(" ,.;") or None,
        operative_date=when,
        affecting_id=affecting,
        affecting_title=(m.group("instrument") or "").strip(" ,.;") or None,
    )


def _take_annotations(unit: ET.Element, provision: str | None) -> list[Annotation]:
    """Pull the ``<div class="annotations">`` blocks out of a provision (removing them
    from the tree, so they don't pollute the operative text) and parse each note."""
    out: list[Annotation] = []
    for parent in list(unit.iter()):  # materialised: the loop removes children
        blocks = [c for c in parent
                  if localname(c.tag).lower() == "div" and c.get("class") == "annotations"]
        for block in blocks:
            for group in block.iter():
                cls = (group.get("class") or "")
                if not cls.endswith("-note") or cls == "annotations":
                    continue
                note_type = cls[0].upper()
                body = " ".join(flow_text(group, line_tags=_LINES).split())
                if body:
                    out.append(parse_annotation(note_type, provision, body))
            parent.remove(block)
    return out


# -- structure ---------------------------------------------------------------
def _child(elem: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in elem if localname(c.tag).lower() == name), None)


def _child_text(elem: ET.Element, name: str) -> str | None:
    child = _child(elem, name)
    if child is None:
        return None
    return " ".join("".join(child.itertext()).split()) or None


def _label(elem: ET.Element, kind: str) -> str:
    num = (_child_text(elem, "number") or "").strip(". ")
    heading = _child_text(elem, "title") or ""
    prefix = {"sect": "s.", "section": "s.", "part": "Part", "chapter": "Chapter",
              "schedule": "Schedule", "article": "Article"}.get(kind, "")
    # Containers usually spell their own name ("PART 1", "CHAPTER 2") in the number or
    # the heading — don't prefix it a second time.
    if kind != "sect" and (num or heading).lower().lstrip("“ ").startswith(kind):
        prefix = ""
    stem = " ".join(p for p in (prefix, num) if p).strip()
    if kind == "schedule" and not num:
        stem = heading.split("\n")[0][:60] if heading else "Schedule"
        heading = ""
    return " ".join(p for p in (stem, heading) if p).strip() or kind


def _flatten_ws(root: ET.Element) -> None:
    """Collapse the source's pretty-print line breaks inside text nodes.

    Both schemas are ``<p>``-based — a newline in a text node is indentation, never
    meaning — but the flat text is assembled line-by-line, so leaving them in wraps
    provisions mid-sentence ("This Act shall\\ncome into operation"). Line structure is
    re-established from the ``<p>`` boundaries instead."""
    for e in root.iter():
        if e.text:
            e.text = re.sub(r"\s+", " ", e.text)
        if e.tail:
            e.tail = re.sub(r"\s+", " ", e.tail)


@dataclass(slots=True)
class _Block:
    label: str
    kind: str
    text: str
    level: int


def _walk(elem: ET.Element, level: int, blocks: list[_Block],
          notes: list[Annotation], relations: list[TypedRelation]) -> None:
    for child in elem:
        name = localname(child.tag).lower()
        if name in _UNITS:
            kind = "schedule" if name == "schedule" else "section"
            label = _label(child, name)
            notes.extend(_take_annotations(child, label))
            _collect_xrefs(child, label, relations)
            text = flow_text(child, skip_tags=_SKIP, line_tags=_LINES)
            if text.strip():
                blocks.append(_Block(label, kind, text, level))
        elif name in _CONTAINERS:
            header = _label(child, name)
            if header:
                blocks.append(_Block(header, name, header, level))
            _walk(child, level + 1, blocks, notes, relations)
        elif name in _PASS:
            _walk(child, level, blocks, notes, relations)


def _collect_xrefs(unit: ET.Element, src_label: str, relations: list[TypedRelation]) -> None:
    for e in unit.iter():
        if localname(e.tag).lower() != "xref":
            continue
        target = _xref_target(e.get("href") or "")
        if target is None:
            continue
        dst_id, anchor = target
        relations.append(TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string=e.get("href"), dst_id=dst_id,
            src_anchor=src_label, dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))


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


def _enactment_date(raw: str | None) -> date | None:
    """``<dateofenactment>`` is YYYYMMDD with no separators (the RDFa and the
    point-in-time URI segment both use YYYY-MM-DD — three formats, one normal form)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


def parse_eisb_xml(data: bytes) -> ParsedDoc:
    source = data.decode("utf-8", errors="replace")
    # An LRC consolidation announces itself by declaring &updatedtodate; — the one
    # signal that separates a revised (non-authoritative) text from an as-enacted one
    # before any content is read.
    revised = "&updatedtodate;" in source[:4000]
    try:
        root = ET.fromstring(decode(data))
    except ET.ParseError:
        return ParsedDoc()

    _flatten_ws(root)
    meta = _child(root, "metadata")
    title = _child_text(meta, "title") if meta is not None else None
    number = _child_text(meta, "number") if meta is not None else None
    year = _child_text(meta, "year") if meta is not None else None
    enacted = _enactment_date(_child_text(meta, "dateofenactment") if meta is not None else None)

    blocks: list[_Block] = []
    notes: list[Annotation] = []
    relations: list[TypedRelation] = []
    _walk(root, 0, blocks, notes, relations)
    if not blocks:  # unrecognised shape — keep the text rather than losing the document
        body = " ".join("".join(root.itertext()).split())
        blocks = [_Block(title or "document", "section", body, 0)]

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

    return ParsedDoc(
        text=SEP.join(parts) or None,
        segments=segments,
        relations=_dedupe(relations),
        title=title,
        decision_date=enacted,
        metadata={"number": number, "year": year, "annotations": notes,
                  "revised": revised, "updated_to": _updated_to(source) if revised else None},
    )


register("eisb-xml", parse_eisb_xml)
