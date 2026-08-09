"""Official explanatory material published alongside UK legislation.

The legislation.gov.uk API has two quite different generations of explanatory
notes: older notes are one structured ``EN`` XML document, while newer notes are
HTML pages linked from a contents page.  Impact assessments are metadata XML plus
the substantive PDF and also have a global Atom feed.  This adapter normalises all
three surfaces without pretending that the material is part of the enacted text.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Tag

from ..core.adapter import BaseAdapter, option_flag
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
from ..extraction import text_or_ocr

BASE_URL = "https://www.legislation.gov.uk"
_ATOM = "{http://www.w3.org/2005/Atom}"
_LEG = "{http://www.legislation.gov.uk/namespaces/legislation}"
_UKM = "{http://www.legislation.gov.uk/namespaces/metadata}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_DCT = "{http://purl.org/dc/terms/}"
_PARENT_RE = re.compile(
    r"/(?:id/)?(?P<id>(?:ukpga|ukla|ukcm|uksi|ukmo|ukci|asp|ssi|anaw|asc|wsi|"
    r"nia|nisr|apni|aosp|aep|mnia)/(?:\d{4}/\d+|[^/?#]+/[^/?#]+/\d+))(?:/|$)", re.I)
_UKIA_RE = re.compile(r"/(?:id/)?(?P<id>ukia/\d{4}/\d+)(?:/|$)", re.I)
_PARENT_IMPACT_RE = re.compile(r"/impacts/(?P<year>\d{4})/(?P<number>\d+)(?:/|$)", re.I)
_NOTES_TYPES = ("notes", "memorandum", "executive-note", "policy-note")
_NOTES_LABELS = {
    "notes": "Explanatory Notes",
    "memorandum": "Explanatory Memorandum",
    "executive-note": "Executive Note",
    "policy-note": "Policy Note",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _iso_date(value: str | None) -> date | None:
    value = _clean(value)[:10]
    try:
        return datetime.fromisoformat(value).date() if value else None
    except ValueError:
        return None


def _impact_title(value: str | None, stable_id: str) -> str:
    title = _clean(value) or stable_id
    return (title if re.search(r"\bimpact assessment\b", title, re.IGNORECASE)
            else f"Impact Assessment: {title}")


def _parent_id(value: str | None) -> str | None:
    match = _PARENT_RE.search(value or "")
    return match.group("id").lower() if match else None


def _related(parent_id: str, relationship: RelationshipType) -> TypedRelation:
    return TypedRelation(
        relationship_type=relationship,
        raw_citation_string=parent_id,
        dst_id=parent_id,
        extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.PENDING,
    )


def parse_impact_feed(raw: bytes) -> tuple[list[Stub], bool]:
    """Parse one page of the official ``/ukia/data.feed`` (pure)."""
    root = ET.fromstring(raw)
    page = int(root.findtext(f"{_LEG}page") or 1)
    more_pages = int(root.findtext(f"{_LEG}morePages") or 0) > page
    stubs: list[Stub] = []
    for entry in root.findall(f"{_ATOM}entry"):
        ident = _UKIA_RE.search(entry.findtext(f"{_ATOM}id") or "")
        if not ident:
            continue
        sid = ident.group("id").lower()
        links = entry.findall(f"{_ATOM}link")
        pdf = next((x.get("href") for x in links
                    if x.get("type") == "application/pdf"), None)
        parent = next((_parent_id(x.get("href")) for x in links
                       if "/impacts" in (x.get("href") or "")), None)
        published = entry.findtext(f"{_ATOM}published")
        stubs.append(Stub(
            stable_id=sid,
            landing_url=f"{BASE_URL}/{sid}",
            raw_url=f"{BASE_URL}/{sid}/data.xml",
            title=_clean(entry.findtext(f"{_ATOM}title")) or None,
            hint_date=_iso_date(published),
            hints={
                "pdf_url": pdf,
                "parent_id": parent,
                "watermark": published,
                "stage": (entry.find(f".//{_UKM}DocumentStage").get("Value")
                          if entry.find(f".//{_UKM}DocumentStage") is not None else None),
                "department": (entry.find(f".//{_UKM}Department").get("Value")
                               if entry.find(f".//{_UKM}Department") is not None else None),
            },
        ))
    return stubs, more_pages


def impact_stubs_for_legislation(raw: bytes, parent_id: str) -> list[Stub]:
    """All impact assessments named by an Act/SI's ``impacts/data.xml``."""
    root = ET.fromstring(raw)
    found: dict[str, Stub] = {}

    def add(url: str | None, title: str | None = None) -> None:
        match = _UKIA_RE.search(url or "")
        parent_match = _PARENT_IMPACT_RE.search(url or "")
        if match:
            sid = match.group("id").lower()
        elif parent_match:
            sid = f"ukia/{parent_match.group('year')}/{parent_match.group('number')}"
        else:
            return
        found.setdefault(sid, Stub(
            stable_id=sid,
            landing_url=f"{BASE_URL}/{sid}",
            raw_url=f"{BASE_URL}/{sid}/data.xml",
            title=_clean(title) or None,
            hints={"parent_id": parent_id},
        ))

    add(root.get("IdURI"), root.findtext(f".//{_DC}title"))
    for link in root.findall(f".//{_ATOM}link"):
        if "/impacts/" in (link.get("href") or ""):
            add(link.get("href"), link.get("title"))
    return list(found.values())


def parse_impact_metadata(raw: bytes) -> dict:
    root = ET.fromstring(raw)
    links = root.findall(f".//{_ATOM}link")
    legislation = root.find(f".//{_UKM}Legislation")
    parent = _parent_id(legislation.get("URI") if legislation is not None else None)
    if not parent:
        parent = next((_parent_id(x.get("href")) for x in links
                       if "/impacts" in (x.get("href") or "")), None)
    pdf = next((x.get("href") for x in links
                if x.get("type") == "application/pdf"), None)
    if not pdf:
        alternative = root.find(f".//{_UKM}Alternative")
        pdf = alternative.get("URI") if alternative is not None else None
    value = lambda name: (root.find(f".//{_UKM}{name}").get("Value")
                          if root.find(f".//{_UKM}{name}") is not None else None)
    return {
        "title": _clean(root.findtext(f".//{_DC}title")) or None,
        "parent_id": parent,
        "pdf_url": pdf,
        "date": _iso_date(root.findtext(f".//{_DCT}valid") or value("Date")),
        "modified": _clean(root.findtext(f".//{_DC}modified")) or None,
        "stage": value("DocumentStage"),
        "department": value("Department"),
    }


def parse_explanatory_notes_xml(raw: bytes) -> tuple[str | None, str, list[Segment]]:
    """Extract old-style ``EN`` XML, retaining its native paragraph numbers."""
    root = ET.fromstring(raw)
    title = _clean(root.findtext(f".//{_DC}title")) or None
    chunks: list[str] = []
    segments: list[Segment] = []

    def append(label: str, value: str, kind: str, level: int) -> None:
        value = _clean(value)
        if not value:
            return
        if chunks:
            chunks.append("\n\n")
        start = sum(map(len, chunks))
        chunks.append(value)
        segments.append(Segment(label=label, char_start=start,
                                char_end=start + len(value), kind=kind, level=level))

    heading_nodes = {
        "Division", "CommentaryPart", "CommentaryDivision", "CommentarySubDivision",
        "CommentaryP1", "CommentarySchedule", "Appendix", "ENprelims",
    }

    def walk(node: ET.Element, level: int = 0) -> None:
        name = _local(node.tag)
        if name == "NumberedPara":
            number = _clean(next((c.text for c in node if _local(c.tag) == "Pnumber"), ""))
            body = _clean(" ".join(
                text for child in node if _local(child.tag) != "Pnumber"
                for text in child.itertext()))
            append(f"para. {number}" if number else "paragraph",
                   f"{number}. {body}" if number else body, "paragraph", level)
            return
        if name in heading_nodes:
            direct_title = next((c for c in node if _local(c.tag) == "Title"), None)
            heading = _clean(" ".join(direct_title.itertext())) if direct_title is not None else ""
            if heading:
                append(heading, heading, "section", level)
                level += 1
        for child in node:
            if _local(child.tag) not in {"Metadata", "Title", "Pnumber"}:
                walk(child, level)

    body = next((x for x in root.iter() if _local(x.tag) == "Body"), None)
    if body is not None:
        walk(body)
    return title, "".join(chunks), segments


def explanatory_notes_metadata(raw: bytes) -> dict:
    """The reader-facing assets carried even by metadata-only ``EN`` XML."""
    root = ET.fromstring(raw)
    links = root.findall(f".//{_ATOM}link")
    pdf = next((x.get("href") for x in links
                if x.get("type") == "application/pdf"), None)
    navigation = next((x for x in links
                       if "/def/navigation/" in (x.get("rel") or "")
                       and not (x.get("rel") or "").endswith("/toc")), None)
    return {
        "instrument_title": _clean(root.findtext(f".//{_DC}title")) or None,
        "pdf_url": pdf,
        "material_title": _clean(navigation.get("title")) if navigation is not None else None,
        "modified": _clean(root.findtext(f".//{_DC}modified")) or None,
    }


def _notes_links(contents: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(contents, "html.parser")
    toc = soup.select_one(".table-of-contents, .ENContents, #viewLegContents")
    links: list[str] = []
    for anchor in (toc or soup).select("a[href]"):
        url = urljoin(base_url, anchor.get("href") or "")
        path = urlsplit(url).path.rstrip("/")
        if re.search(r"/notes/(?:division|annex)/\d", path) and url not in links:
            links.append(url)
    return links


def _parse_notes_html_page(raw: bytes) -> list[tuple[str, str, str, int]]:
    """Return ``(label, text, kind, level)`` units from one modern notes page."""
    soup = BeautifulSoup(raw, "html.parser")
    content = soup.select_one(".en-content article, .main-content-full article")
    if content is None:
        return []
    out: list[tuple[str, str, str, int]] = []
    for node in content.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "li"]):
        if node.name and node.name.startswith("h"):
            heading = _clean(node.get_text(" ", strip=True))
            if heading:
                out.append((heading, heading, "section", int(node.name[1]) - 1))
            continue
        if not isinstance(node, Tag) or node.name != "li":
            continue
        owner = node.find_parent("ol")
        if owner is None or not owner.has_attr("start") or node.find_parent("li") is not None:
            continue
        siblings = [x for x in owner.find_all("li", recursive=False)]
        try:
            number = int(node.get("value") or owner.get("start") or 1) + siblings.index(node)
        except (TypeError, ValueError):
            continue
        body = _clean(node.get_text(" ", strip=True))
        if body:
            out.append((f"para. {number}", f"{number}. {body}", "paragraph", 1))
    return out


def combine_notes_html(pages: list[bytes]) -> tuple[str, list[Segment]]:
    chunks: list[str] = []
    segments: list[Segment] = []
    seen_paragraphs: set[str] = set()
    seen_headings: set[str] = set()
    for page in pages:
        for label, value, kind, level in _parse_notes_html_page(page):
            key = label.casefold()
            seen = seen_paragraphs if kind == "paragraph" else seen_headings
            if key in seen:
                continue
            seen.add(key)
            if chunks:
                chunks.append("\n\n")
            start = sum(map(len, chunks))
            chunks.append(value)
            segments.append(Segment(label=label, char_start=start,
                                    char_end=start + len(value), kind=kind, level=level))
    return "".join(chunks), segments


class UKLegislationMaterialsAdapter(BaseAdapter):
    source = "uk-legislation-materials"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 notes: object = None, impacts: object = None,
                 client: RateLimitedClient | None = None) -> None:
        if isinstance(ids, str):
            ids = tuple(x.strip().strip("/") for x in ids.split(",") if x.strip())
        self.ids = tuple(ids or ())
        self.notes = option_flag(notes, True)
        self.impacts = option_flag(impacts, True)
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            for requested in self.ids:
                if requested.startswith("ukia/"):
                    yield Stub(stable_id=requested, landing_url=f"{BASE_URL}/{requested}",
                               raw_url=f"{BASE_URL}/{requested}/data.xml")
                    continue
                parent = requested.split("@", 1)[0]
                if self.notes:
                    # ``notes`` is always attempted: modern primary legislation has
                    # HTML pages but deliberately 404s its XML representation. The
                    # other three types are yielded only when their metadata advertises
                    # a substantive PDF, avoiding three phantom records per Act.
                    yield Stub(stable_id=f"{parent}/notes",
                               landing_url=f"{BASE_URL}/{parent}/notes",
                               raw_url=f"{BASE_URL}/{parent}/notes/data.xml",
                               hints={"kind": "notes", "notes_type": "notes",
                                      "parent_id": parent})
                    for notes_type in _NOTES_TYPES[1:]:
                        url = f"{BASE_URL}/{parent}/{notes_type}/data.xml"
                        response = self._client.get(url, raise_for_4xx=False)
                        if response.status_code >= 400:
                            continue
                        try:
                            metadata = explanatory_notes_metadata(response.content)
                        except ET.ParseError:
                            continue
                        if not metadata.get("pdf_url"):
                            continue
                        yield Stub(
                            stable_id=f"{parent}/{notes_type}",
                            landing_url=f"{BASE_URL}/{parent}/{notes_type}",
                            raw_url=url,
                            title=metadata.get("material_title"),
                            hints={"kind": "notes", "notes_type": notes_type,
                                   "parent_id": parent, "metadata_raw": response.content},
                        )
                if self.impacts:
                    response = self._client.get(
                        f"{BASE_URL}/{parent}/impacts/data.xml", raise_for_4xx=False)
                    if response.status_code < 400 and response.content.startswith(b"<"):
                        yield from impact_stubs_for_legislation(response.content, parent)
            return

        # Explanatory notes have no global discovery feed. Impact assessments do.
        if not self.impacts:
            return
        page = 1
        pages = 0
        while True:
            response = self._client.get(f"{BASE_URL}/ukia/data.feed", params={"page": page})
            stubs, more = parse_impact_feed(response.content)
            for stub in stubs:
                watermark = stub.hints.get("watermark")
                if since and watermark and watermark <= since:
                    return
                yield stub
            pages += 1
            if not more or (max_pages is not None and pages >= max_pages):
                return
            page += 1

    def fetch(self, stub: Stub) -> Record | None:
        if stub.hints.get("kind") == "notes" or stub.stable_id.endswith("/notes"):
            return self._fetch_notes(stub)
        return self._fetch_impact(stub)

    def _fetch_notes(self, stub: Stub) -> Record | None:
        parent = stub.hints.get("parent_id") or stub.stable_id.removesuffix("/notes")
        notes_type = stub.hints.get("notes_type") or stub.stable_id.rsplit("/", 1)[-1]
        xml_raw = stub.hints.get("metadata_raw")
        xml = (None if xml_raw is not None
               else self._client.get(stub.raw_url, raise_for_4xx=False))
        xml_status = 200 if xml_raw is not None else xml.status_code
        xml_content = xml_raw if xml_raw is not None else xml.content
        is_en_xml = False
        if xml_status < 400 and xml_content.lstrip().startswith(b"<"):
            try:
                is_en_xml = _local(ET.fromstring(xml_content).tag) == "EN"
            except ET.ParseError:
                pass
        if is_en_xml:
            title, text, segments = parse_explanatory_notes_xml(xml_content)
            metadata = explanatory_notes_metadata(xml_content)
            raw, ext, fmt = xml_content, "xml", "legislation-en-xml"
            download_url = metadata.get("pdf_url")
            extraction_engine = None
            needs_ocr = False
            # Explanatory memoranda/notes for secondary legislation often expose
            # metadata only in XML; the linked PDF is the actual document.
            if not text and download_url:
                pdf = self._client.get(urljoin(BASE_URL, download_url), raise_for_4xx=False)
                if pdf.status_code < 400 and pdf.content.startswith(b"%PDF"):
                    text, needs_ocr, page_spans, extraction_engine = text_or_ocr(pdf.content)
                    text = text.strip()
                    segments = [Segment(label=f"p. {page}", char_start=start,
                                        char_end=end, kind="page")
                                for page, start, end in page_spans]
                    raw, ext, fmt = pdf.content, "pdf", "pdf"
            instrument_title = metadata.get("instrument_title")
            label = metadata.get("material_title") or _NOTES_LABELS.get(notes_type, "Explanatory material")
            if ((not title or title == instrument_title)
                    and not re.match(r"(?i)^(?:explanatory|executive|policy)\b", title or "")):
                title = f"{label} to {instrument_title}" if instrument_title else label
            if not text and not download_url:
                # A generic /notes endpoint for an SI can be a metadata shell which
                # merely says that a separate /memorandum exists. Discovery emits that
                # real companion independently; do not store the shell as a duplicate.
                return None
        else:
            if notes_type != "notes":
                return None
            contents_url = f"{BASE_URL}/{parent}/notes/contents"
            contents = self._client.get(contents_url, raise_for_4xx=False)
            if contents.status_code >= 400:
                return None
            title_soup = BeautifulSoup(contents.content, "html.parser")
            act_title = (_clean((title_soup.find("meta", attrs={"name": "Title"}) or {}).get("content"))
                         if title_soup.find("meta", attrs={"name": "Title"}) else "")
            if not act_title:
                act_title = _clean(title_soup.title.get_text(" ") if title_soup.title else "")
                act_title = re.sub(r"\s*[—-]\s*Explanatory Notes\s*$", "", act_title, flags=re.I)
            title = f"Explanatory Notes to {act_title}" if act_title else None
            links = _notes_links(contents.content, str(contents.url))
            pages: list[bytes] = []
            if links:
                for url in links:
                    response = self._client.get(url, raise_for_4xx=False)
                    if response.status_code < 400:
                        pages.append(response.content)
            else:
                # Some markdown-era contents URLs redirect to division 1. Follow the
                # publisher's next links; this also works when there is no separate TOC.
                current = contents
                visited: set[str] = set()
                while current.status_code < 400 and str(current.url) not in visited:
                    visited.add(str(current.url))
                    pages.append(current.content)
                    soup = BeautifulSoup(current.content, "html.parser")
                    nxt = soup.select_one(".prevNextNav li.next a[href]")
                    if nxt is None:
                        break
                    current = self._client.get(urljoin(str(current.url), nxt.get("href") or ""),
                                               raise_for_4xx=False)
            text, segments = combine_notes_html(pages)
            if not text:
                return None
            raw = b"\n<!-- raglex:next-notes-page -->\n".join(pages)
            ext, fmt = "html", "legislation-notes-html"
            download_url = None
            extraction_engine = None
            needs_ocr = False
        if not text:
            # Keep the official metadata/asset record and route it to the existing
            # fetched-no-text/OCR queue instead of treating a scan as nonexistent.
            text = None
        parent_kind = "act" if parent.startswith(("ukpga/", "asp/", "nia/", "anaw/", "asc/")) else "named"
        local_aliases = ({"the Act": parent, "this Act": parent} if parent_kind == "act"
                         else {"the Regulations": parent, "these Regulations": parent,
                               "the instrument": parent})
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.NOTE,
            title=title or f"Explanatory Notes to {parent}",
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=ext,
            text=text,
            segments=segments,
            relations=[_related(parent, RelationshipType.ANALYSES)],
            extracted_via=ExtractedVia.STRUCTURED,
            extra={
                "format": fmt,
                "material_type": "explanatory_notes",
                "notes_type": notes_type,
                "parent_legislation": parent,
                # Reader-facing links are deliberately separate from the XML/stitched
                # HTML used to ingest the record. APIs and static exports can offer a
                # page a person can browse, not an implementation representation.
                "html_url": stub.landing_url,
                "contents_url": f"{BASE_URL}/{parent}/notes/contents",
                "download_url": download_url,
                "source_modified": (metadata.get("modified") if is_en_xml else None),
                "extraction_engine": extraction_engine,
                "needs_ocr": needs_ocr,
                "citation_default_instrument": {"id": parent, "kind": parent_kind},
                "citation_local_aliases": local_aliases,
            },
        )

    def _fetch_impact(self, stub: Stub) -> Record | None:
        metadata_response = self._client.get(stub.raw_url)
        metadata = parse_impact_metadata(metadata_response.content)
        pdf_url = metadata.get("pdf_url") or stub.hints.get("pdf_url")
        if not pdf_url:
            return None
        pdf = self._client.get(urljoin(BASE_URL, pdf_url), raise_for_4xx=False)
        if pdf.status_code >= 400 or not pdf.content.startswith(b"%PDF"):
            return None
        # Many older assessments are image-only scans. A plain PDF extraction returns
        # an empty string without error, so escalate here instead of silently dropping
        # an official document that the feed and metadata both prove exists.
        text, needs_ocr, page_spans, engine = text_or_ocr(pdf.content)
        text = text.strip()
        segments = [Segment(label=f"p. {page}", char_start=start, char_end=end,
                            kind="page") for page, start, end in page_spans]
        parent = metadata.get("parent_id") or stub.hints.get("parent_id")
        title = _impact_title(metadata.get("title") or stub.title, stub.stable_id)
        # Feed titles are often only a subject (``Communications Data``), which is
        # opaque in mixed search results. Preserve the official wording but identify
        # what the record is; avoid doubling titles that already say it themselves.
        extra = {
            "format": "pdf",
            "material_type": "impact_assessment",
            "download_url": str(pdf.url),
            "html_url": stub.landing_url,
            "source_modified": metadata.get("modified"),
            "stage": metadata.get("stage") or stub.hints.get("stage"),
            "department": metadata.get("department") or stub.hints.get("department"),
            "extraction_engine": engine,
            "needs_ocr": needs_ocr,
        }
        relations: list[TypedRelation] = []
        if parent:
            relations.append(_related(parent, RelationshipType.RELATED_TO))
            parent_kind = ("act" if parent.startswith(
                ("ukpga/", "asp/", "nia/", "anaw/", "asc/")) else "named")
            local_aliases = ({"the Act": parent, "this Act": parent}
                             if parent_kind == "act" else {
                                 "the Regulations": parent,
                                 "these Regulations": parent,
                                 "the instrument": parent,
                             })
            extra.update({
                "parent_legislation": parent,
                "citation_default_instrument": {"id": parent, "kind": parent_kind},
                "citation_local_aliases": local_aliases,
            })
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.PREPARATORY,
            title=title,
            decision_date=metadata.get("date") or stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=pdf.content,
            raw_ext="pdf",
            text=text or None,
            segments=segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra=extra,
        )
