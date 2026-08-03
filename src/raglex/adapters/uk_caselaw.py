"""UK Find Case Law (The National Archives) adapter — Atom + LegalDocML.

The cleanest no-auth starting source (§2, §9 step 1). Discovery is the Atom feed;
content is per-document Akoma Ntoso XML. See the API reference for the contract:

- Base:           https://caselaw.nationalarchives.gov.uk
- Atom feed:      GET /atom.xml?court=...&order=-date&page=N&per_page=50
                  paginate via <link rel="next">
- Document XML:   GET /{document_uri}/data.xml  → Akoma Ntoso LegalDocML
- Change signal:  <tna:contenthash> (text-level SHA-256) in each Atom entry
- Rate limit:     1,000 requests / rolling 5 min per IP → HTTP 429

Parsing is split from HTTP (``parse_atom`` / ``parse_judgment`` are pure) so the
adapter is testable against fixture XML with no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import re
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

BASE_URL = "https://caselaw.nationalarchives.gov.uk"

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_TNA_NS = "{https://caselaw.nationalarchives.gov.uk}"
# Akoma Ntoso namespace varies by version; match on local-name to stay robust.


@dataclass(frozen=True, slots=True)
class AtomPage:
    stubs: list[Stub]
    next_url: str | None


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _is_junk_cite(cite: str) -> bool:
    """Drop non-citations the markup carries as ``<ref>`` — empty anchors (``#``),
    in-page fragments, javascript/mailto links — so they never reach the worklist."""
    if len(cite) < 3:
        return True
    low = cite.lower()
    return cite.startswith("#") or low.startswith(("javascript:", "mailto:", "tel:"))


def _document_uri_from_url(url: str) -> str:
    """`https://.../uksc/2024/123` or `.../d-{uuid}` → `uksc/2024/123`."""
    path = url.removeprefix(BASE_URL).strip("/")
    # entry ids sometimes carry a trailing /data.xml or a fragment; strip those
    for suffix in ("/data.xml", "/data.html"):
        path = path.removesuffix(suffix)
    return path


def court_from_slug(stable_id: str | None) -> str | None:
    """The court from a Find Case Law document slug — its first segment
    (``ewca/civ/2003/1045`` → ``ewca``, ``ukut/aac/2012/440`` → ``ukut``). None for the
    opaque new-style ``d-{uuid}`` ids, which carry no court in the path."""
    if not stable_id or stable_id.startswith("d-") or "/" not in stable_id:
        return None
    head = stable_id.split("/", 1)[0].lower()
    return head if head.isalpha() else None


def _parse_atom_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_atom(xml_bytes: bytes) -> AtomPage:
    """Parse one Atom feed page into stubs + the next-page URL (pure)."""
    root = ET.fromstring(xml_bytes)
    next_url: str | None = None
    for link in root.findall(f"{_ATOM_NS}link"):
        if link.get("rel") == "next":
            next_url = link.get("href")

    stubs: list[Stub] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip() or None
        updated = entry.findtext(f"{_ATOM_NS}updated")
        entry_id = (entry.findtext(f"{_ATOM_NS}id") or "").strip()

        landing = entry_id
        for link in entry.findall(f"{_ATOM_NS}link"):
            if link.get("rel") in (None, "alternate") and link.get("href"):
                landing = link.get("href")
                break

        if not landing:
            continue
        document_uri = _document_uri_from_url(landing)
        court = document_uri.split("/", 1)[0] if "/" in document_uri else None

        # The full <updated> timestamp is the incremental cursor. A date-only cursor
        # loses same-day arrivals FOREVER: the watermark lands on today's date, so a
        # judgment published later today compares <= and the crawl stops before it.
        hints: dict = {}
        if updated:
            hints["watermark"] = updated.strip()
        # <tna:contenthash> is FCL's change signal — carried so a held judgment that
        # was revised upstream (anonymisation, corrections) is re-fetched, not skipped.
        contenthash = (entry.findtext(f"{_TNA_NS}contenthash") or "").strip()
        if contenthash:
            hints["contenthash"] = contenthash

        stubs.append(
            Stub(
                stable_id=document_uri,
                landing_url=landing if landing.startswith("http") else f"{BASE_URL}/{document_uri}",
                raw_url=f"{BASE_URL}/{document_uri}/data.xml",
                hint_date=_parse_atom_date(updated),
                title=title,
                court=court,
                hints=hints,
            )
        )
    return AtomPage(stubs=stubs, next_url=next_url)


def _iter_text(elem: ET.Element) -> Iterator[str]:
    if elem.text and elem.text.strip():
        yield elem.text.strip()
    for child in elem:
        yield from _iter_text(child)
        if child.tail and child.tail.strip():
            yield child.tail.strip()


def _direct_child(elem: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in elem if _localname(child.tag) == name), None)


_BLOCK_TEXT_TAGS = {
    "num", "heading", "content", "intro", "wrapup", "p", "paragraph",
    "subparagraph", "level",
}


def _normalised_mixed_text(elem: ET.Element, excluded: set[int] | None = None) -> str:
    """XML mixed content without inventing spaces between inline styling runs.

    Structural children do need a boundary (``<num>I.</num><content>Overview``), while
    inline ``span`` children emphatically do not (``<span>Ő</span><span>sterreich``).
    """
    excluded = excluded or set()
    out: list[str] = []

    def visit(node: ET.Element) -> None:
        if id(node) in excluded:
            return
        if node.text:
            out.append(node.text)
        for child in node:
            if id(child) in excluded:
                if child.tail:
                    out.append(child.tail)
                continue
            if (_localname(child.tag).lower() in _BLOCK_TEXT_TAGS and out
                    and out[-1] and not out[-1][-1].isspace()):
                out.append(" ")
            visit(child)
            if child.tail:
                out.append(child.tail)

    visit(elem)
    return " ".join("".join(out).split())


def _compact_text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    # The mixed-content walk preserves whether adjacent styled runs actually had
    # whitespace between them. Joining the individually stripped runs with a space corrupted
    # names and quotation marks in FCL judgments, e.g. ``Ő`` + ``sterreichische``
    # became ``Ő sterreichische`` and ``(“`` + ``FF`` + ``”)`` became ``(“ FF ”)``.
    # Collapse only whitespace that exists in the XML; never manufacture it at an
    # inline element boundary.
    return _normalised_mixed_text(elem)


def _fully_styled_heading(elem: ET.Element) -> bool:
    """Whether a short structural unit is formatted wholly as a heading.

    Find Case Law's LegalDocML converter commonly places the *next* heading in a
    ``<subparagraph>`` at the end of the preceding numbered paragraph.  It is not
    safe to treat every subparagraph as a heading (most are ordinary (a)/(b) list
    items), so require the text-bearing ``p`` itself, or its sole text child, to
    carry bold/italic/underline formatting.
    """
    p = next((e for e in elem.iter() if _localname(e.tag) == "p"), None)
    if p is None:
        return False
    if (p.get("class") or "").lower().startswith("heading"):
        return True
    style = (p.get("style") or "").lower()
    styled = ("font-weight:bold", "font-style:italic", "text-decoration-line:underline")
    if any(token in style for token in styled):
        return True
    substantive = [
        child for child in p
        if _compact_text(child) and _localname(child.tag) not in {"marker"}
    ]
    outside = (p.text or "").strip() or any((c.tail or "").strip() for c in p)
    if len(substantive) != 1 or outside:
        return False
    child_style = (substantive[0].get("style") or "").lower()
    return any(token in child_style for token in styled)


def _is_embedded_heading(elem: ET.Element) -> bool:
    if _localname(elem.tag) not in {"subparagraph", "level"}:
        return False
    content = _direct_child(elem, "content")
    heading = _compact_text(content)
    host = content if content is not None else elem
    return bool(heading and len(heading) <= 240 and _fully_styled_heading(host))


def _text_skipping(elem: ET.Element, excluded: set[int]) -> str:
    """Compact element text while omitting selected structural descendants."""
    return _normalised_mixed_text(elem, excluded)


def _judgment_blocks(body: ET.Element) -> list[tuple[str, str, str]]:
    """The judgment's own paragraphs and headings, in source order.

    Quoted legislation and authorities are themselves marked up with nested
    ``<paragraph>`` elements.  They are content of the enclosing judgment paragraph,
    not new citable paragraphs of the judgment.  The old ``body.iter()`` scan flattened
    both levels, producing duplicate/out-of-order labels such as quoted ``“1.`` and
    ``“40.`` between paragraphs 62 and 90.  Walk the tree instead and stop descending
    once a top-level judgment paragraph has been emitted.
    """
    blocks: list[tuple[str, str, str]] = []

    def add_paragraph(paragraph: ET.Element, n: int) -> None:
        embedded = [e for e in paragraph.iter() if _is_embedded_heading(e)]
        excluded = {id(e) for e in embedded}
        label_node = _direct_child(paragraph, "num")
        label = _compact_text(label_node) or f"para {n}"
        main = _text_skipping(paragraph, excluded).strip()

        # An unnumbered wrapper paragraph carrying a Roman-numeral, wholly styled
        # title is itself a heading (the first "I. Overview" in many FCL files).
        roman_heading = bool(
            paragraph.get("eId") is None
            and re.fullmatch(r"[IVXLC]+\.?", label, re.IGNORECASE)
            and len(main) <= 260
            and _fully_styled_heading(paragraph)
        )
        blocks.append((label, "heading" if roman_heading else "paragraph", main))
        for heading in embedded:
            h_label = _compact_text(_direct_child(heading, "num"))
            h_text = _compact_text(heading)
            blocks.append((h_label or h_text, "heading", h_text))

    number = 0

    def walk(elem: ET.Element) -> None:
        nonlocal number
        for child in elem:
            name = _localname(child.tag)
            if name == "paragraph":
                number += 1
                add_paragraph(child, number)
                continue                    # nested quote paragraphs stay in its text
            if name == "heading":
                text = _compact_text(child)
                if text:
                    blocks.append((text, "heading", text))
                continue
            walk(child)

    walk(body)
    return blocks


def judgment_judges(xml_bytes: bytes) -> list[str]:
    """Judges identified by LegalDocML's semantic ``<judge>`` elements."""
    root = ET.fromstring(xml_bytes)
    names = [_compact_text(e) for e in root.iter() if _localname(e.tag) == "judge"]
    return list(dict.fromkeys(name for name in names if name))


def parse_judgment(
    xml_bytes: bytes,
) -> tuple[str, list[TypedRelation], str | None, list[Segment]]:
    """Extract text, citation edges, neutral citation, and structural segments from
    a LegalDocML judgment (pure). Akoma Ntoso writes judgments as numbered
    `<paragraph>`s — the native citable unit ("[42]") — so those become the
    segments (§6b). Edges start as ``mentions``; §1.3a refines the type later."""
    root = ET.fromstring(xml_bytes)

    ncn: str | None = None
    relations: list[TypedRelation] = []

    for elem in root.iter():
        name = _localname(elem.tag)
        if name == "FRBRname" and ncn is None:
            ncn = elem.get("value")
        elif name == "neutralCitation" and ncn is None:
            ncn = (elem.text or "").strip() or None
        elif name == "ref":
            cite = (elem.get("href") or "".join(_iter_text(elem))).strip()
            if _is_junk_cite(cite):
                continue
            # Normalise to a resolvable candidate at emit time (a legislation/caselaw
            # URL → its id), so the edge resolves the moment its target is harvested
            # and the worklist shows a routable reference, not a raw URL (§5b).
            from ..resolve.matchers import first_candidate

            cand = first_candidate(cite)
            relations.append(
                TypedRelation(
                    relationship_type=RelationshipType.MENTIONS,
                    raw_citation_string=cite,
                    dst_id=cand.value if cand else None,
                    extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.PENDING,
                )
            )

    body = next((e for e in root.iter() if _localname(e.tag) == "judgmentBody"), root)
    blocks = _judgment_blocks(body)
    if not blocks:
        blocks = blocks_by_localname(body, {"p"}, kind="paragraph", counter_label="para")
    if not blocks:
        blocks = [("body", "section", element_text(body))]
    text, segments = assemble(blocks)
    return text, relations, ncn, segments


class UKCaseLawAdapter(BaseAdapter):
    """UK Find Case Law adapter. ``court`` filters the feed (e.g. ``ukftt/grc`` for
    the info-rights/DP tribunal)."""

    source = "uk-caselaw"
    # 1,000 req / 5 min ≈ 3.3/s; stay comfortably under (§1.8, API ref).
    min_interval = 0.4
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        court: str | None = None,
        query: str | None = None,
        per_page: int = 50,
        source_key: str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.court = court
        self.source = source_key or (
            "uk-grc" if court == "ukftt/grc" else type(self).source
        )
        # Find Case Law supports full-text search on the Atom feed (?query=…), so a
        # keyword watch can limit the harvest at the source (not just post-filter).
        self.query = query
        self.per_page = per_page
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # Incremental crawls sort by -transformation ("body last modified", which is what
        # the entry <updated> element carries) so the cursor field IS the sort field. The
        # old -date sort orders by court publication date while the cursor compared
        # <updated> — non-monotonic, so a late-published old judgment (decided last year,
        # added to FCL yesterday) sat below the stop line and was never seen. First/full
        # crawls keep -date (newest decisions first is the right seeding order).
        order = "-transformation" if since else "-date"
        params: dict[str, object] = {"order": order, "per_page": self.per_page}
        if self.court:
            params["court"] = self.court
        if self.query:
            params["query"] = self.query
        url = f"{BASE_URL}/atom.xml"
        pages = 0
        while url:
            resp = self._client.get(url, params=params if pages == 0 else None)
            page = parse_atom(resp.content)
            for stub in page.stubs:
                # Incremental cursor: stop once we reach docs at/older than the watermark
                # (feed is newest-first). Compare the FULL timestamp — a date-only cursor
                # skips everything else published the same day. An old date-only watermark
                # ("2026-07-13") still compares correctly against a timestamp (same-day
                # items re-fetch once, then dedup).
                wm = stub.hints.get("watermark") or (
                    stub.hint_date.isoformat() if stub.hint_date else None)
                if since and wm and wm <= since:
                    return
                yield stub
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return
            url = page.next_url

    def fetch(self, stub: Stub) -> Record | None:
        resp = self._client.get(stub.raw_url)
        raw = resp.content
        text, relations, ncn, segments = parse_judgment(raw)
        judges = judgment_judges(raw)
        extra = {"contenthash": stub.hints["contenthash"]} if stub.hints.get("contenthash") else {}
        if judges:
            extra["judges"] = judges
            extra["judge"] = judges[0]
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=stub.title or ncn,
            # a targeted single-item fetch (radiate/discover) often has no court on the
            # stub — but the FCL slug names it (ewca/civ/2003/1045 → ewca), so derive it.
            court=stub.court or court_from_slug(stub.stable_id),
            decision_date=stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="xml",
            text=text or None,
            segments=segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            # the feed's contenthash rides along so the next crawl can tell a revised
            # judgment (hash changed upstream) from one we already hold
            extra=extra,
        )
