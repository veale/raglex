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

# ``gertyp`` is the court field in the official RII DTD.  Keep one canonical,
# human-readable value in ``documents.court`` so facets do not split between an
# abbreviation from the bulk feed and a long name from another German source.
_COURT_NAMES = {
    "BVerfG": "Bundesverfassungsgericht",
    "BGH": "Bundesgerichtshof",
    "BAG": "Bundesarbeitsgericht",
    "BFH": "Bundesfinanzhof",
    "BSG": "Bundessozialgericht",
    "BVerwG": "Bundesverwaltungsgericht",
    "BPatG": "Bundespatentgericht",
    "GmSOGB": "Gemeinsamer Senat der obersten Gerichtshöfe des Bundes",
}


def rii_court_name(value: str | None) -> str | None:
    """Canonical display name for an RII ``gertyp`` token."""
    if not value:
        return None
    value = " ".join(value.split())
    return _COURT_NAMES.get(value, value)


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
    # Real RII documents carry ``gertyp`` and ``spruchkoerper``.  ``gericht`` is
    # accepted as a compatibility fallback for older fixtures/derived exports.
    court_code = _text(doc, "gertyp") or _text(doc, "gericht")
    court = rii_court_name(court_code)
    court_body = _text(doc, "spruchkoerper")
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
        metadata={"ecli": ecli, "court": court, "court_code": court_code,
                  "court_body": court_body, "aktenzeichen": aktenzeichen,
                  "doktyp": _text(doc, "doktyp"),
                  "doknr": _text(doc, "doknr") or doc.get("doknr")},
    )


register("rii-xml", parse_rii)
