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
    # These two header fields contain high-value, explicit citation edges.  They
    # must be part of the derived text or the citation scanner never sees them.
    ("norm", "Normen"),
    ("vorinstanz", "Vorinstanz"),
    ("leitsatz", "Leitsatz"),
    ("orientierungssatz", "Orientierungssatz"),
    ("sonstosatz", "Sonstiger Orientierungssatz"),
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


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if localname(c.tag).lower() == name), None)


def _zone_blocks(el: ET.Element, label: str) -> list[tuple[str, str, str]]:
    """A judgment zone → assemble() blocks. juris rii encodes numbered paragraphs as
    ``<dl class="RspDL"><dt><a name="rd_N">N</a></dt><dd>…</dd></dl>`` — so where a zone
    carries them, emit one **paragraph** block per ``<dd>`` (its Randnummer, from the
    ``<dt>``, as the label) instead of flattening the whole zone into one blob. Flattening
    was what buried the paragraph breaks and pulled every Randnummer inline into the body.
    A zone with no RspDL (Leitsatz, Tenor, header fields) stays a single zone block."""
    dls = [c for c in el.iter()
           if localname(c.tag).lower() == "dl" and "RspDL" in (c.get("class") or "")]
    if not dls:
        body = " ".join(element_text(el).split())
        return [(label, "zone", body)] if body else []
    out: list[tuple[str, str, str]] = [(label, "heading", label)]  # section heading line
    for dl in dls:
        dt, dd = _child(dl, "dt"), _child(dl, "dd")
        rn = " ".join(element_text(dt).split()) if dt is not None else ""
        body = " ".join(element_text(dd).split()) if dd is not None else ""
        if body:
            out.append((rn or label, "paragraph", body))
    return out


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
    court_location = _text(doc, "gerort")
    aktenzeichen = _text(doc, "aktenzeichen") or _text(doc, "az")
    doc_date = _iso(_text(doc, "entsch-datum") or _text(doc, "entscheidungsdatum"))
    title = _text(doc, "titelzeile") or ", ".join(x for x in (court, aktenzeichen) if x) or ecli

    blocks: list[tuple[str, str, str]] = []
    for tag, label in _ZONES:
        el = _find(doc, tag)
        if el is None:
            continue
        blocks.extend(_zone_blocks(el, label))
    text, segments = assemble(blocks)

    identifier = _text(doc, "identifier")
    return ParsedDoc(
        text=text or None,
        segments=segments,
        title=title,
        decision_date=doc_date,
        metadata={"ecli": ecli, "court": court, "court_code": court_code,
                  "court_body": court_body, "aktenzeichen": aktenzeichen,
                  "doktyp": _text(doc, "doktyp"),
                  "doknr": _text(doc, "doknr") or doc.get("doknr"),
                  "court_location": court_location,
                  "norms": _text(doc, "norm"),
                  "prior_instance": _text(doc, "vorinstanz"),
                  "region": _text(doc, "region"),
                  "identifier": identifier,
                  "coverage": _text(doc, "coverage"),
                  "source_language": _text(doc, "language"),
                  "publisher": _text(doc, "publisher"),
                  "access_rights": _text(doc, "accessRights")},
    )


register("rii-xml", parse_rii)
