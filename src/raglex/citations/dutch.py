"""Dutch legal references: extract locally, resolve against Rechtspraak/BWB/LiDO ids.

The graph keeps a BWB work as the destination and the exact provision as its anchor.
When a Juriconnect reference supplies a ``g``/``z`` date, that date is retained both
in the candidate (so a point-in-time copy can be harvested) and in the anchor.
"""

from __future__ import annotations

import re
import unicodedata

from .models import Citation


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in value if not unicodedata.combining(c)).casefold()


def law_name_alias(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", _fold(value)).strip()
    # BWB publishes the civil code as separate works titled "Burgerlijk Wetboek
    # Boek 1" … "Boek 10", while citations use the collective abbreviation BW.
    key = re.sub(r"^burgerlijk wetboek boek \d+$", "burgerlijk wetboek", key)
    return f"nl:law:{key}"


def ljn_alias(value: str) -> str:
    return "nl:ljn:" + re.sub(r"\s+", "", value or "").upper()


_LAW_NAMES = {
    "BW": "Burgerlijk Wetboek", "Awb": "Algemene wet bestuursrecht",
    "Sr": "Wetboek van Strafrecht", "Sv": "Wetboek van Strafvordering",
    "Gw": "Grondwet", "Rv": "Wetboek van Burgerlijke Rechtsvordering",
    "Vw": "Vreemdelingenwet 2000", "Wob": "Wet openbaarheid van bestuur",
    "Woo": "Wet open overheid", "UAVG": "Uitvoeringswet AVG",
}
_LAW_ALT = "|".join(sorted((re.escape(x) for x in _LAW_NAMES), key=len, reverse=True))


def _pin(article: str | None, paragraph: str | None = None,
         sub: str | None = None, date: str | None = None) -> str | None:
    if not article:
        return None
    out = f"Artikel {article}"
    if paragraph:
        out += f", lid {paragraph}"
    if sub:
        out += f", onder {sub}"
    if date:
        out += f" (geldend op {date})"
    return out


# Full Juriconnect references occur both as plain text and inside overheid.nl URLs.
JURICONNECT_RE = re.compile(
    r"(?P<jci>jci1\.3:c:(?P<bwb>BWBR\d{7}))"
    r"(?P<params>(?:[?&;](?:amp;)?(?:hoofdstuk|artikel|lid|onderdeel|g|z)=[^\s&#;]+)*)",
    re.IGNORECASE,
)


def _params(raw: str | None) -> dict[str, str]:
    return {k.casefold(): v for k, v in re.findall(
        r"(hoofdstuk|artikel|lid|onderdeel|g|z)=([^&;\s#]+)", raw or "", re.I)}


def juriconnect_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for m in JURICONNECT_RE.finditer(text):
        p = _params(m.group("params"))
        effective = p.get("g") or p.get("z")
        bwb = m.group("bwb").upper()
        candidate = f"{bwb}@{effective}" if effective else bwb
        out.append(Citation(
            raw=m.group(0), entity_kind="act", candidate_id=candidate,
            pinpoint=_pin(p.get("artikel"), p.get("lid"), p.get("onderdeel"), effective),
            char_start=m.start(), char_end=m.end(), method="nl_juriconnect", confidence=1.0,
        ))
    return out


# ``artikel 6:162 BW``, ``art. 8:42, eerste lid, Awb`` and ``artikel 10 Grondwet``.
LAW_REFERENCE_RE = re.compile(
    rf"\b(?:art(?:ikel)?\.?\s+)(?P<article>\d{{1,3}}(?::\d{{1,4}})?[a-z]?)"
    rf"(?:\s*,?\s*(?P<lid>\d+|eerste|tweede|derde|vierde|vijfde)\s+lid)?"
    rf"(?:\s*,?\s*(?:van\s+)?(?:de|het)?\s*)?(?P<law>{_LAW_ALT})\b", re.I)


def law_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    for m in LAW_REFERENCE_RE.finditer(text):
        title = _LAW_NAMES.get(next((k for k in _LAW_NAMES if k.casefold() == m.group("law").casefold()), ""), m.group("law"))
        out.append(Citation(
            raw=m.group(0), entity_kind="act", candidate_id=law_name_alias(title),
            pinpoint=_pin(m.group("article"), m.group("lid")), char_start=m.start(),
            char_end=m.end(), method="nl_law_reference", confidence=.97,
        ))
    return out


LJN_RE = re.compile(r"\b(?:LJN|LJ[N]?[- ]?nummer|ELRO)\s*[:.= -]*\s*(?P<id>[A-Z]{2}\s*\d{4})\b", re.I)


def ljn_citations(text: str) -> list[Citation]:
    return [Citation(raw=m.group(0), entity_kind="case", candidate_id=ljn_alias(m.group("id")),
                     pinpoint=None, char_start=m.start(), char_end=m.end(),
                     method="nl_ljn", confidence=.98) for m in LJN_RE.finditer(text)]


def dutch_citations(text: str) -> list[Citation]:
    return juriconnect_citations(text) + law_citations(text) + ljn_citations(text)
