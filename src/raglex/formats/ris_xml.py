"""Austria's RIS judgment XML — the ``risdok`` markup behind every RIS document.

The Rechtsinformationssystem des Bundes serves every judgment, Rechtssatz and authority
decision as the same house XML, whatever the deciding body:

```xml
<risdok><metadaten/><nutzdaten><abschnitt>
    <kzinhalt>…</kzinhalt><fzinhalt>…</fzinhalt>      <- page header/footer furniture
    <ueberschrift typ="titel">Spruch</ueberschrift>
    <absatz typ="erltext" ct="spruch">…</absatz>
    <ueberschrift typ="titel">Text</ueberschrift>
    <absatz typ="erltext" ct="text"><gs> [1] </gs>…</absatz>
</abschnitt></nutzdaten><layoutdaten/></risdok>
```

Two attributes carry the whole structure and both are needed:

* ``ueberschrift typ="titel"`` is the **zone heading** RIS itself prints — *Gericht*,
  *Geschäftszahl*, *Kopf*, *Spruch*, *Text*, *Rechtssatz*, *Norm*, *Beachte*,
  *Leitsatz*, *Entscheidungstexte*, *European Case Law Identifier*. It is the reader's
  outline.
* ``absatz/@ct`` is the **machine name** of the zone the paragraph belongs to, and it
  survives where the heading is missing. Both are recorded: the heading becomes the
  segment label, the ``ct`` becomes the zone key in ``metadata['zones']``.

## The paragraph number is inside the text, not on the element

An Austrian judgment numbers its reasons ``[1]``, ``[2]``, … and RIS puts that marker in
the paragraph's own text (usually wrapped in a ``<gs>`` letter-spacing span) rather than
on an attribute. Since the Randnummer is what a later judgment pinpoints — "6 Ob 127/20z
Rz 14" — it is lifted out and becomes the segment label, exactly as ``rii_xml`` and
``olg_html`` do for German Randnummern. Paragraphs without one keep their zone's label.

## Metadata zones are not body text

``Gericht``, ``Geschäftszahl``, ``Entscheidungsdatum``, ``Dokumentnummer`` and
``European Case Law Identifier`` restate what the OGD API already gave the adapter as
structured fields. They are parsed into ``metadata`` and kept OUT of the flat text, so a
full-text search for a docket does not match every document that merely prints one, and
the reader opens on the judgment rather than on a header block. ``Norm`` is the one
metadata zone kept in the text as well: it is the list of provisions the decision turns
on, written in citable form, and the citation grammar should see it.

## The furniture

``kzinhalt``/``fzinhalt`` are the printed page header and footer ("www.ris.bka.gv.at
Seite 1 von 2"). They repeat on every page of the RTF/PDF rendition and carry no
content; parsing them in put the site's domain name and a page count into the middle of
every judgment. They are dropped, as is ``layoutdaten``.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from ..core.models import Segment
from ..core.segmentation import assemble, localname
from .base import ParsedDoc, register

#: Structural furniture that is not document content.
_SKIP = frozenset({"kzinhalt", "fzinhalt", "layoutdaten", "metadaten", "abstand"})

#: RIS spells a handful of characters as elements rather than as entities.
_GLYPH = {"gdash": "–", "nbsp": " ", "amp": "&", "gt": ">", "lt": "<",
          "br": "\n", "tab": " ", "wechsel": " "}

#: Inline formatting RIS marks up, mapped to the vocabulary ``Segment.formatting`` uses.
_FORMAT = {"b": "bold", "i": "italic", "u": "underline"}

#: Zones that restate the OGD metadata. Read into ``metadata``, kept out of the text —
#: see the module docstring. ``norm`` is deliberately NOT here.
_META_ZONES = {
    "gericht": "court", "gz": "docket", "entscheidungsdatum": "decision_date",
    "ecli": "ecli", "dokumentnummer": "document_number", "organ": "body",
    "rechtssatznummer": "rechtssatz_number", "slgnr": "collection_number",
    "geschaeftszahl": "docket", "senat": "panel", "entscheidungsart": "disposition",
}

#: "[1]" / "[12]" opening a paragraph — the Randnummer a later decision pinpoints.
_RANDNUMMER = re.compile(r"^\s*\[\s*(\d{1,4})\s*\]\s*")

#: A Rechtssatz's sub-proposition marker, "(T5)", which RIS puts at the END of the rider
#: it qualifies. It is the anchor the Entscheidungstexte apparatus points at ("nur T5",
#: "Beisatz wie T7"), so a reader that loses it cannot follow the treatment edges back
#: into the text.
_SUBPROP = re.compile(r"\(T(\d{1,3})\)\s*$")


def _inline(elem: ET.Element, out: list[str], marks: list[dict],
            active: tuple[str, ...] = ()) -> None:
    """Flatten one element's inline content, recording formatting spans as it goes.

    Offsets are collected relative to the paragraph being built and are rebased onto the
    document text by ``parse_ris``; a formatting run recorded against the wrong origin
    would underline an unrelated sentence.
    """
    def emit(chunk: str) -> None:
        if not chunk:
            return
        start = sum(len(p) for p in out)
        out.append(chunk)
        for kind in active:
            marks.append({"kind": kind, "start": start, "end": start + len(chunk)})

    name = localname(elem.tag).lower()
    if name in _GLYPH:
        emit(_GLYPH[name])
    elif elem.text:
        emit(elem.text)
    for child in elem:
        child_name = localname(child.tag).lower()
        if child_name in _SKIP:
            continue
        nested = active + (_FORMAT[child_name],) if child_name in _FORMAT else active
        _inline(child, out, marks, nested)
        if child.tail:
            start = sum(len(p) for p in out)
            out.append(child.tail)
            for kind in active:
                marks.append({"kind": kind, "start": start, "end": start + len(child.tail)})


def _paragraph(elem: ET.Element) -> tuple[str, list[dict]]:
    out: list[str] = []
    marks: list[dict] = []
    _inline(elem, out, marks)
    raw = "".join(out)
    # Collapse runs of spaces but keep the explicit newlines <br/> introduced.
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in raw.split("\n")]
    text = "\n".join(line for line in lines if line)
    if text == raw:
        return text, marks
    # Whitespace normalisation moved every offset; rather than tracking the shift
    # character by character, re-find each marked run in the cleaned text. A run that no
    # longer appears (it was pure whitespace) is dropped rather than guessed at.
    rebased: list[dict] = []
    for mark in marks:
        fragment = re.sub(r"\s+", " ", raw[mark["start"]:mark["end"]]).strip()
        if not fragment:
            continue
        at = text.find(fragment)
        if at >= 0:
            rebased.append({"kind": mark["kind"], "start": at, "end": at + len(fragment)})
    return text, rebased


def _zone_label(heading: str | None, ct: str | None) -> str:
    if heading:
        return heading
    return (ct or "text").replace("_", " ").title()


def parse_ris(data: bytes) -> ParsedDoc:
    """A RIS ``risdok`` document → flat text, zone/paragraph segments and metadata."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc(text=None)

    blocks: list[tuple[str, str, str, int]] = []
    formatting: list[list[dict]] = []
    metadata: dict = {}
    zones: list[str] = []
    heading: str | None = None
    current_zone: str | None = None

    def walk(elem: ET.Element) -> None:
        nonlocal heading, current_zone
        for child in elem:
            name = localname(child.tag).lower()
            if name in _SKIP:
                continue
            if name == "ueberschrift":
                text, _ = _paragraph(child)
                heading = text or None
                continue
            if name == "absatz":
                ct = (child.get("ct") or "").strip().lower() or None
                text, marks = _paragraph(child)
                if not text:
                    continue
                if ct and ct != current_zone:
                    current_zone = ct
                    if ct not in zones:
                        zones.append(ct)
                    if heading and ct not in _META_ZONES:
                        blocks.append((heading, "heading", heading, 0))
                        formatting.append([])
                if ct in _META_ZONES:
                    metadata.setdefault(_META_ZONES[ct], text)
                    heading = None
                    continue
                label = _zone_label(heading, ct)
                kind = "zone"
                if rand := _RANDNUMMER.match(text):
                    label = f"[{int(rand.group(1))}]"
                    kind = "paragraph"
                    text = text[rand.end():]
                elif sub := _SUBPROP.search(text):
                    label = f"T{int(sub.group(1))}"
                    kind = "paragraph"
                blocks.append((label, kind, text, 1 if kind == "paragraph" else 0))
                formatting.append(marks)
                heading = None
                continue
            if name == "table":
                text, marks = _paragraph(child)
                if text:
                    blocks.append((_zone_label(heading, current_zone), "zone", text, 1))
                    formatting.append(marks)
                heading = None
                continue
            walk(child)

    for section in root.iter():
        if localname(section.tag).lower() == "nutzdaten":
            walk(section)

    text, segments = assemble(blocks)
    # ``assemble`` drops blocks that reduce to nothing; re-pair the surviving segments
    # with their formatting by walking the two lists together on the kept text.
    kept = [(i, block) for i, block in enumerate(blocks) if (block[2] or "").strip()]
    with_formatting: list[Segment] = []
    for segment, (index, _block) in zip(segments, kept):
        marks = formatting[index]
        if not marks:
            with_formatting.append(segment)
            continue
        offset = segment.char_start
        with_formatting.append(Segment(
            label=segment.label, char_start=segment.char_start,
            char_end=segment.char_end, kind=segment.kind, level=segment.level,
            formatting=tuple({"kind": m["kind"], "start": offset + m["start"],
                              "end": offset + m["end"]} for m in marks
                             if offset + m["end"] <= segment.char_end),
        ))
    if zones:
        metadata["zones"] = zones
    return ParsedDoc(text=text or None, segments=with_formatting, metadata=metadata)


register("ris-xml", parse_ris)
