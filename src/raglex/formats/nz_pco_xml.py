"""New Zealand PCO legislation XML parser — schema-tolerant by design.

The Parliamentary Counsel Office publishes NZ legislation in a **PCO-specific,
DTD-defined schema**, and this parser could not be verified against a live sample: the
legislation website returns an HTTP 405 human-verification wall to plain *and* stealth
requests, and ``catalogue.data.govt.nz`` (which hosts the bulk XML) sits behind the same
kind of interstitial. Defeating either is both fragile and discourteous, and the
sanctioned channel — the Developer API — needs a key that is still pending.

So rather than hard-code guesses at element names that may well be wrong, this parser
**infers structure from shape**, which holds across DTD revisions and is exactly the
property an unverified schema needs:

* a **unit** (a citable provision) is any element carrying a numbering child
  (``label``/``num``/``number``) — that is what makes a provision addressable;
* a **container** (Part, Subpart, Schedule, cross-heading) is any element carrying a
  heading child but no numbering of its own;
* everything else is descended through.

Known PCO names (``prov``, ``subprov``, ``crosshead``, ``sched``…) are supplied as hints
that reinforce the inference, not as the sole basis for it. The critical guarantee is the
**fallback**: if nothing structural is recognised, the whole document's text is still
captured as one block, so an unexpected schema costs structure, never content.

When the API key arrives, verify against a real file and tighten this — the metadata it
emits (``inferred_structure``) says whether inference or the fallback did the work, so a
corpus parsed before verification is auditable rather than silently suspect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from xml.etree import ElementTree as ET

from ..core.models import Segment
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
# Sub-units that start a new line so enumerated provisions read as a list.
_LINES = {"subprov", "para", "subpara", "item", "def", "p", "label", "list-item"}

_DOCTYPE_RE = re.compile(r"<!DOCTYPE.*?(?:\[.*?\])?\s*>", re.S)
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
            text = flow_text(child, skip_tags=_LABELS | _HEADINGS, line_tags=_LINES)
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
                text = flow_text(child, skip_tags=_HEADINGS, line_tags=_LINES)
                if text.strip():
                    blocks.append(_Block(header or name, "section", text, level + 1))
        else:
            _walk(child, level, blocks)


def parse_nz_pco_xml(data: bytes) -> ParsedDoc:
    source = data.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(expand_entities(source))
    except ET.ParseError:
        return ParsedDoc()

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
        relations=[],   # NZ cross-reference markup is unverified — see module docstring
        title=title,
        decision_date=as_at,
        metadata={
            "root": localname(root.tag).lower(),
            "as_at": as_at,
            # Whether shape-inference found structure, or the whole-text fallback ran.
            # Lets a pre-verification corpus be audited once a real sample is available.
            "inferred_structure": inferred,
            "unverified_schema": True,
        },
    )


register("nz-pco-xml", parse_nz_pco_xml)
