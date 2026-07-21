"""juris "rii" XML parser — the rechtsprechung-im-internet case-law format.

The federal-court decision bulk (behind ``de-rii``): one ``<dokument>`` per decision
against the juris rii DTD, carrying ECLI, court, date, file number, and the judgment
text split into functional fields (Leitsatz, Tenor, Tatbestand, Entscheidungsgründe…).
Those fields become native chunk ``Segment``s (§6b) — the same shape the NeuRIS JSON
case law produces, so the legacy bulk and the live portal write the same records.

Verify against a real rii document before a backfill; the field set below follows the
documented DTD but is read by local-name so namespace/casing drift is tolerated.
"""

from __future__ import annotations

from datetime import date
from xml.etree import ElementTree as ET

from ..core.segmentation import assemble, element_text, localname
from .base import ParsedDoc, register

# rii text fields → German zone labels, in a judgment's layout order.
_ZONES = (
    ("leitsatz", "Leitsatz"),
    ("orientierungssatz", "Orientierungssatz"),
    ("tenor", "Tenor"),
    ("tatbestand", "Tatbestand"),
    ("entscheidungsgruende", "Entscheidungsgründe"),
    ("gruende", "Gründe"),
    ("abwmeinung", "Abweichende Meinung"),
    ("sonstlt", "Sonstiger Langtext"),
)


def _find(root: ET.Element, tag: str) -> ET.Element | None:
    return next((e for e in root.iter() if localname(e.tag).lower() == tag), None)


def _text(root: ET.Element, tag: str) -> str | None:
    el = _find(root, tag)
    if el is None:
        return None
    return " ".join(element_text(el).split()) or None


def _iso(value: str | None) -> date | None:
    if not value:
        return None
    v = value.strip()
    try:
        # rii dates are compact YYYYMMDD ("20100108"); accept ISO too.
        if len(v) >= 8 and v[:8].isdigit():
            return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def parse_rii(data: bytes) -> ParsedDoc:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ParsedDoc()
    doc = _find(root, "dokument")
    if doc is None:
        doc = root

    ecli = _text(doc, "ecli")
    court = _text(doc, "gericht")
    aktenzeichen = _text(doc, "aktenzeichen") or _text(doc, "az")
    doc_date = _iso(_text(doc, "entsch-datum") or _text(doc, "entscheidungsdatum"))
    title = _text(doc, "titelzeile") or ", ".join(x for x in (court, aktenzeichen) if x) or ecli

    blocks: list[tuple[str, str, str]] = []
    for tag, label in _ZONES:
        el = _find(doc, tag)
        if el is None:
            continue
        body = " ".join(element_text(el).split())
        if body:
            blocks.append((label, "zone", body))
    text, segments = assemble(blocks)

    return ParsedDoc(
        text=text or None,
        segments=segments,
        title=title,
        decision_date=doc_date,
        metadata={"ecli": ecli, "court": court, "aktenzeichen": aktenzeichen,
                  "doktyp": _text(doc, "doktyp"), "doknr": doc.get("doknr")},
    )


register("rii-xml", parse_rii)
