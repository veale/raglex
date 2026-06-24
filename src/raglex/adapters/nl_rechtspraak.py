"""Netherlands — Rechtspraak Open Data adapter (REST/XML, ECLI-native).

The cleanest Tier-1 source (§2): a two-step model — query the ECLI **index**
(`/uitspraken/zoeken`, Atom) for a set of ECLIs, then fetch each judgment's
**content** (`/uitspraken/content?id=ECLI`, RDF + body). Crucially, Rechtspraak
hands you a **citation graph for free**: each decision's `dcterms:relation`
("Formele relatie") carries the target ECLI plus a typed treatment code
(`aanleg`/`gevolg`) — so we mint *typed* edges (§1.3a), not bare ones, and the
ECLI destinations resolve directly (§5b).

This is the adapter that exercises the cross-jurisdiction premise: an ECLI-native
NL source feeding the exact same pipeline as the UK Atom/LegalDocML source, with
the multilingual topic gate (§4) handling Dutch (persoonsgegevens / AVG).

Parsing is split from HTTP (`parse_index` / `parse_content` are pure) so the
adapter is testable against fixture XML with no network.

Rate limit: max 10 req/s (§A) — `min_interval` set accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    Stub,
    TypedRelation,
)
from ..core.segmentation import assemble, blocks_by_localname, element_text

ZOEKEN_URL = "https://data.rechtspraak.nl/uitspraken/zoeken"
CONTENT_URL = "https://data.rechtspraak.nl/uitspraken/content"

_ATOM_NS = "{http://www.w3.org/2005/Atom}"

# FormeleRelaties `gevolg` (outcome) → treatment type (§1A). The relation runs
# from the citing (later) decision to the earlier instance it reviews; the gevolg
# says how the later court treated it. Unknown/absent → `mentions` (§1.3a: even
# writing only `mentions` at first is fine — the typed column being present is
# what matters).
_GEVOLG_TYPE = {
    "bekrachtiging/bevestiging": RelationshipType.APPLIES,  # affirmed
    "vernietiging": RelationshipType.OVERRULES,  # quashed/reversed
    "niet-ontvankelijk": RelationshipType.CONSIDERS,
}


@dataclass(frozen=True, slots=True)
class IndexPage:
    stubs: list[Stub]
    count: int  # how many entries this page held (0 → end of pagination)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr_by_suffix(elem: ET.Element, suffix: str) -> str | None:
    """Read an attribute by local-name suffix, namespace-agnostic (the RDF here
    mixes the e-justice ECLI and psi.rechtspraak namespaces)."""
    for key, value in elem.attrib.items():
        if key.rsplit("}", 1)[-1] == suffix:
            return value
    return None


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_index(xml_bytes: bytes) -> IndexPage:
    """Parse one `/zoeken` Atom page into stubs (pure). The entry id is the ECLI;
    `<updated>` is the modified-cursor used as the incremental watermark."""
    root = ET.fromstring(xml_bytes)
    stubs: list[Stub] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        ecli = (entry.findtext(f"{_ATOM_NS}id") or "").strip()
        if not ecli.startswith("ECLI:"):
            continue
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip() or None
        updated = entry.findtext(f"{_ATOM_NS}updated")
        # title shape: "ECLI:..., <Court>, <dd-mm-yyyy>, <case-nos>"
        court = None
        if title:
            parts = [p.strip() for p in title.split(",")]
            if len(parts) >= 2:
                court = parts[1]
        stubs.append(
            Stub(
                stable_id=ecli,
                landing_url=f"https://uitspraken.rechtspraak.nl/details?id={ecli}",
                raw_url=f"{CONTENT_URL}?id={ecli}",
                hint_date=_parse_iso(updated),  # modified cursor (watermark)
                title=title,
                court=court,
            )
        )
    return IndexPage(stubs=stubs, count=len(stubs))


def _iter_text(elem: ET.Element) -> Iterator[str]:
    if elem.text and elem.text.strip():
        yield elem.text.strip()
    for child in elem:
        yield from _iter_text(child)
        if child.tail and child.tail.strip():
            yield child.tail.strip()


def _map_relation(elem: ET.Element) -> TypedRelation | None:
    """A `dcterms:relation` ('Formele relatie') → a typed edge to an ECLI node."""
    dst = _attr_by_suffix(elem, "resourceIdentifier")
    if not dst or not dst.startswith("ECLI:"):
        return None
    gevolg = _attr_by_suffix(elem, "gevolg") or ""
    # The code lives in the URI fragment (.../gevolg#bekrachtiging/bevestiging);
    # take it whole — don't split on the '/' inside the code itself.
    gevolg_key = (gevolg.rsplit("#", 1)[-1] if "#" in gevolg else gevolg).lower()
    rel_type = _GEVOLG_TYPE.get(gevolg_key, RelationshipType.MENTIONS)
    label = (elem.text or "").strip() or None
    return TypedRelation(
        relationship_type=rel_type,
        raw_citation_string=label or dst,
        dst_id=dst,
        extracted_via=ExtractedVia.STRUCTURED,
        # ECLI dst → confirmed against the catalogue by the resolver, not here.
        resolution_status=ResolutionStatus.PENDING,
    )


@dataclass(frozen=True, slots=True)
class ParsedContent:
    ecli: str | None
    title: str | None
    court: str | None
    decision_date: date | None
    rechtsgebied: str | None
    text: str | None
    relations: list[TypedRelation]
    segments: list[Segment] = field(default_factory=list)


def parse_content(xml_bytes: bytes) -> ParsedContent:
    """Parse a `/content` document into metadata + typed edges + body (pure)."""
    root = ET.fromstring(xml_bytes)
    ecli = title = court = rechtsgebied = None
    decision_date: date | None = None
    relations: list[TypedRelation] = []
    body_el: ET.Element | None = None

    for elem in root.iter():
        name = _localname(elem.tag)
        label = _attr_by_suffix(elem, "label") or ""
        text = (elem.text or "").strip()
        if name == "identifier" and ecli is None and text.startswith("ECLI:"):
            ecli = text
        elif name == "creator" and label == "Instantie":
            court = text or court
        elif name == "date" and label == "Uitspraakdatum":
            decision_date = _parse_iso(text) or decision_date
        elif name == "title" and title is None and text:
            title = text
        elif name == "subject" and label == "Rechtsgebied":
            rechtsgebied = text or rechtsgebied
        elif name == "relation":
            rel = _map_relation(elem)
            if rel is not None:
                relations.append(rel)
        elif name in ("uitspraak", "conclusie") and body_el is None:
            body_el = elem

    segments: list[Segment] = []
    body: str | None = None
    if body_el is not None:
        blocks = blocks_by_localname(body_el, {"para", "p"}, kind="paragraph", counter_label="para")
        if not blocks:
            blocks = [("body", "section", element_text(body_el))]
        body, segments = assemble(blocks)
    return ParsedContent(
        ecli=ecli,
        title=title,
        court=court,
        decision_date=decision_date,
        rechtsgebied=rechtsgebied,
        text=body or None,
        segments=segments,
        relations=relations,
    )


class NLRechtspraakAdapter(BaseAdapter):
    source = "nl-rechtspraak"
    # 10 req/s allowed; pace under it (§1.8, §A).
    min_interval = 0.12
    requires_js = False
    requires_proxy = False

    def __init__(self, *, per_page: int = 100, client: RateLimitedClient | None = None) -> None:
        self.per_page = per_page
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        offset = 0
        pages = 0
        while True:
            params = {"return": "DOC", "max": self.per_page, "from": offset}
            if since:
                params["modified"] = since  # incremental by modified timestamp
            resp = self._client.get(ZOEKEN_URL, params=params)
            page = parse_index(resp.content)
            if page.count == 0:
                return
            yield from page.stubs
            offset += page.count
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return

    def fetch(self, stub: Stub) -> Record | None:
        resp = self._client.get(stub.raw_url)
        raw = resp.content
        parsed = parse_content(raw)
        return Record(
            source=self.source,
            stable_id=parsed.ecli or stub.stable_id,  # ECLI is the primary key (§1.1)
            ecli=parsed.ecli or stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=parsed.title or stub.title,
            court=parsed.court or stub.court,
            decision_date=parsed.decision_date or stub.hint_date,
            language="nl",
            source_language="nl",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="xml",
            text=parsed.text,
            segments=parsed.segments,
            relations=parsed.relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={"rechtsgebied": parsed.rechtsgebied} if parsed.rechtsgebied else {},
        )
