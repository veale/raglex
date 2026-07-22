"""German extract → normalise helpers.

German statutory references are one-to-many.  ``bundesrecht.normalise`` expands
ranges, i.V.m. joins and compact sub-provision lists before we create graph edges.
The destination is the federal law Work (the same ``de/gesetz/<jurabk>`` id minted by
GII); the exact canonical provision is retained as ``dst_anchor``.
"""

from __future__ import annotations

import re
import unicodedata

from bundesrecht import normalise

from .models import Citation


def law_id(abbreviation: str) -> str:
    folded = unicodedata.normalize("NFKD", abbreviation or "")
    slug = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return "de/gesetz/" + re.sub(r"[^a-z0-9]+", "", slug)


def normalise_docket(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").upper()
    return re.sub(r"\s+", " ", value.replace("–", "-").replace("—", "-")).strip(" ,.;")


def case_alias(court: str, docket: str) -> str:
    court_raw = re.sub(r"[^A-ZÄÖÜ0-9]+", "", (court or "").upper())
    court_key = {
        "BUNDESVERFASSUNGSGERICHT": "BVERFG",
        "BUNDESGERICHTSHOF": "BGH",
        "BUNDESARBEITSGERICHT": "BAG",
        "BUNDESFINANZHOF": "BFH",
        "BUNDESSOZIALGERICHT": "BSG",
        "BUNDESVERWALTUNGSGERICHT": "BVERWG",
        "BUNDESPATENTGERICHT": "BPATG",
    }.get(court_raw, court_raw)
    docket_key = re.sub(r"[^A-Z0-9/.-]+", "", normalise_docket(docket))
    return f"de:case:{court_key}:{docket_key}"


# Deliberately bounded.  It starts on §/Art., accepts only provision vocabulary, and
# ends on a law abbreviation.  This avoids the unbounded legal-regex failure mode that
# previously wedged whole-corpus rescans.
_PARA = r"\d{1,5}[a-z]?"
_SUB = (r"(?:Abs(?:atz)?\.?|S(?:atz)?\.?|Nrn?\.?|Nummer|Buchst(?:abe)?\.?|"
        r"Alt(?:ernative)?\.?|Halbs(?:atz)?\.?|HS\.?)\s*(?:\d+[a-z]?|[a-z]|[IVX]+)")
_TAIL = rf"(?:\s*(?:{_SUB}|[,;]|und\b|oder\b|bis\s+{_PARA}|[-–—]\s*{_PARA}|f{{1,2}}\.))*"
_ONE = rf"(?:§§?|Art(?:ikel|\.)?)\s*{_PARA}{_TAIL}"
_COMPACT_ONE = rf"(?:§|Art(?:ikel|\.)?)\s*{_PARA}\s+(?:[IVX]{{1,4}}|\(\d+\))\s+\d+"
_IVM = rf"(?:\s+i\.?\s*V\.?\s*m\.?\s+{_ONE})?"
_LAW = r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9]*(?:\s+(?:[IVX]{1,4}|\d{1,2}))?"
LAW_REFERENCE_RE = re.compile(
    rf"(?P<raw>(?:{_COMPACT_ONE}|{_ONE}{_IVM})\s+(?P<law>{_LAW}))", re.IGNORECASE)


def _expand_compact(raw: str) -> str:
    """§ 19 IV 1 / § 19 (4) 1 → the explicit form bundesrecht canonicalises."""
    def _arabic(value: str) -> str:
        if value.isdigit():
            return value
        total, previous = 0, 0
        for ch in reversed(value.upper()):
            current = {"I": 1, "V": 5, "X": 10}[ch]
            total += current if current >= previous else -current
            previous = current
        return str(total)

    return re.sub(
        r"^((?:§|Art\.)\s*\d+[a-z]?)\s+(?:\((\d+)\)|([IVX]+))\s+(\d+)\s+",
        lambda m: f"{m.group(1)} Abs. {_arabic(m.group(2) or m.group(3))} Satz {m.group(4)} ",
        raw, flags=re.IGNORECASE,
    )


def _canonical_parts(canonical: str) -> tuple[str, str] | None:
    m = re.match(r"^(?P<prefix>§|Art\.)\s+(?P<body>.+?)\s+(?P<law>[A-ZÄÖÜ][\wÄÖÜäöüß]*(?:\s+\d+)?)$",
                 canonical)
    if not m:
        return None
    return m.group("law"), f"{m.group('prefix')} {m.group('body')}"


def law_citations(text: str) -> list[Citation]:
    found: list[Citation] = []
    for match in LAW_REFERENCE_RE.finditer(text):
        raw = match.group("raw")
        # CEDH is the French abbreviation for the European Convention/Court, not a
        # German statute abbreviation. In French judgments ``§ 95, CEDH 19`` is a
        # Strasbourg paragraph/report marker; treating it as de/gesetz/cedh19 creates
        # a cross-jurisdiction phantom node. German texts use EMRK for the Convention.
        if re.match(r"CEDH\b", match.group("law"), re.IGNORECASE):
            continue
        try:
            canonical_refs = normalise(_expand_compact(raw))
        except (ValueError, TypeError):
            continue
        for canonical in dict.fromkeys(canonical_refs):
            parts = _canonical_parts(canonical)
            if not parts:
                continue
            law, pinpoint = parts
            found.append(Citation(
                raw=raw, entity_kind="act", candidate_id=law_id(law), pinpoint=pinpoint,
                char_start=match.start(), char_end=match.end(), method="de_law_reference",
                confidence=1.0,
            ))
    return found


_COURT = r"BVerfG|BGH|BAG|BFH|BSG|BVerwG|BPatG|EuGH|OLG\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]+|LG\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]+"
_DOCKET = (r"(?:(?:\d+|[IVX]+)\s+)?(?:BvR|BvL|BvF|BvQ|BVR|StR|ZR|ZB|AR|AZR|ABR|R|C|CN|B|W|U|L|K|O)"
           r"\s+\d{1,6}(?:[./-]\d{1,4})+")
CASE_REFERENCE_RE = re.compile(
    rf"\b(?P<court>{_COURT})\b[^;\n]{{0,100}}?(?P<docket>{_DOCKET})\b"
    rf"(?:\s*,?\s*Rn\.?\s*(?P<rn>\d+(?:\s*(?:ff?\.|[-–,])\s*\d*)?))?", re.IGNORECASE)


def case_citations(text: str) -> list[Citation]:
    return [Citation(
        raw=m.group(0).strip(), entity_kind="case",
        candidate_id=case_alias(m.group("court"), m.group("docket")),
        pinpoint=f"Rn. {m.group('rn')}" if m.group("rn") else None,
        char_start=m.start(), char_end=m.end(), method="de_case_reference", confidence=0.95,
    ) for m in CASE_REFERENCE_RE.finditer(text)]


def german_citations(text: str) -> list[Citation]:
    return law_citations(text) + case_citations(text)
