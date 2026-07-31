"""DMA policy material from the Commission's Digital Markets Act site.

Two surfaces that the ``dma-cases`` register (specification proceedings and
non-compliance decisions) does not cover: the Article 35 annual reports, and the public
consultations — where the draft guidelines, the templates and the published submissions
live. Both are ordinary Europa Component Library pages, so both are read by the same
card parser.

Documents are keyed on the **Commission document UUID**, the same identity
``eu-consumer-guidance`` uses. That is deliberate: a document published on more than one
Commission site is one document, and keying on the UUID makes it dedupe rather than
appear twice under two site-specific ids.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://digital-markets-act.ec.europa.eu"
CONSULTATIONS = f"{BASE}/public-consultations_en"
# The Drupal node feed behind the consultations page — the keep-current path.
CONSULTATIONS_RSS = f"{BASE}/node/44/rss_en"
ANNUAL_REPORTS = f"{BASE}/about-dma/dma-annual-reports_en"

_DOC_RE = re.compile(
    r"/document(?:/download)?/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def _date(value: str | None) -> date | None:
    text = " ".join((value or "").split())
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:30].strip(), fmt).date()
        except ValueError:
            continue
    match = re.search(r"\b(\d{1,2}\s+[A-Za-z]+\s+(?:19|20)\d{2})\b", text)
    if match:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(match.group(1), fmt).date()
            except ValueError:
                pass
    return None


def document_cards(html: bytes | str, *, page_url: str) -> list[dict]:
    """Every Commission document linked from an ECL page, one dict per UUID.

    A card renders the same document twice — once as a landing link and once as a
    download button — so the UUID is the key and the direct download wins, saving a
    request. The card also carries the publication date and the real document title,
    which the bare link text ("Download") does not.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}
    for link in soup.select("a[href]"):
        match = _DOC_RE.search(str(link.get("href") or ""))
        if not match:
            continue
        uuid = match.group(1).lower()
        url = urljoin(page_url, str(link["href"]))
        card = link.find_parent(class_="ecl-file") or link.find_parent("article")
        title_node = card.select_one(".ecl-file__title") if card else None
        title = (title_node.get_text(" ", strip=True) if title_node
                 else link.get("data-untranslated-label") or link.get_text(" ", strip=True))
        # A card's meta list leads with the publication TYPE ("General publications")
        # and only then the date, so take the first item that actually parses as one.
        published: date | None = None
        for meta in (card.select(".ecl-file__detail-meta-item, time") if card else []):
            published = _date(str(meta.get("datetime") or "")
                              or meta.get_text(" ", strip=True))
            if published:
                break
        is_download = "/document/download/" in url
        previous = found.get(uuid)
        if previous and not is_download:
            continue
        found[uuid] = {
            "uuid": uuid,
            "url": url,
            "landing_url": f"{BASE}/document/{uuid}_en",
            "title": " ".join(str(title or "").split()) or None,
            "date": published or (previous or {}).get("date"),
            "origin_page": page_url,
            "direct_download": is_download,
        }
    return list(found.values())


def consultation_links(html: bytes | str) -> list[str]:
    """The consultation pages linked from the index, English only.

    Every Europa page repeats itself in 24 languages under the same slug with a
    different ``_xx`` suffix; harvesting all of them would multiply the corpus by 24
    for no added law.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for link in soup.select("a[href]"):
        href = str(link.get("href") or "").split("#", 1)[0]
        if "consultation" not in href or not href.endswith("_en"):
            continue
        url = urljoin(BASE, href)
        if urlsplit(url).netloc != urlsplit(BASE).netloc:
            continue
        path = urlsplit(url).path
        if path.rstrip("/") in ("/public-consultations_en", "/consultations_en"):
            continue
        if url not in out:
            out.append(url)
    return out


def rss_links(xml: bytes | str) -> list[tuple[str, date | None]]:
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out: list[tuple[str, date | None]] = []
    for item in root.iter("item"):
        url = (item.findtext("link") or "").strip()
        if not url:
            continue
        published = None
        raw = (item.findtext("pubDate") or "").strip()
        if raw:
            from email.utils import parsedate_to_datetime
            try:
                published = parsedate_to_datetime(raw).date()
            except (TypeError, ValueError):
                published = None
        out.append((url, published))
    return out


def page_text(html: bytes | str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("main") or soup.select_one("article") or soup
    for tag in body.select("script, style, nav, form, header, footer, .ecl-menu"):
        tag.decompose()
    return "\n".join(" ".join(line.split())
                     for line in body.get_text("\n").splitlines() if line.strip())


class _DMADocumentsAdapter(BaseAdapter):
    """Shared: read index pages → Commission document UUIDs → PDFs."""

    doc_type = DocType.GUIDANCE
    tags: tuple[str, ...] = ()
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)

    def _index_pages(self, since: str | None) -> Iterator[str]:
        raise NotImplementedError

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        seen: set[str] = set()
        pages = list(self._index_pages(since))
        if max_pages is not None:
            pages = pages[:max_pages]
        for page_url in pages:
            try:
                response = self._client.get(page_url)
            except FetchError:
                continue
            for card in document_cards(response.content, page_url=str(response.url)):
                if card["uuid"] in seen:
                    continue
                seen.add(card["uuid"])
                published = card.get("date")
                yield Stub(
                    stable_id=f"eu/commission/document/{card['uuid']}",
                    landing_url=card["landing_url"], raw_url=card["url"],
                    title=card.get("title"), court="European Commission",
                    hint_date=published,
                    hints={
                        "uuid": card["uuid"], "origin_page": card["origin_page"],
                        "watermark": published.isoformat() if published else None,
                    },
                )

    def fetch(self, stub: Stub) -> Record | None:
        try:
            response = self._client.get(stub.raw_url)
        except FetchError:
            return None
        blob = response.content
        if blob.startswith(b"%PDF"):
            try:
                extracted = extract_bytes(blob, ext="pdf", mime="application/pdf")
            except ValueError:
                return None
            text, raw, ext = (extracted.text or "").strip(), blob, "pdf"
        else:
            text, raw, ext = page_text(blob), blob, "html"
        if len(text) < 120:
            return None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=self.doc_type,
            title=stub.title,
            court="European Commission",
            decision_date=stub.hint_date,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["dma", "digital-markets-act", *self.tags],
            extra={
                "jurisdiction": "eu",
                "document_uuid": stub.hints.get("uuid"),
                "origin_page": stub.hints.get("origin_page"),
                "require_recognized_legal_citation": False,
            },
        )


class DMAConsultationsAdapter(_DMADocumentsAdapter):
    source = "dma-consultations"
    tags = ("consultation",)

    def _index_pages(self, since: str | None) -> Iterator[str]:
        """Index pages to read.

        The RSS feed carries only the consultations themselves, so an incremental run
        reads it and visits just the new ones. A backfill walks the index page, which
        lists the closed consultations the feed has dropped.
        """
        if since:
            try:
                feed = self._client.get(CONSULTATIONS_RSS).content
            except FetchError:
                feed = b""
            for url, published in rss_links(feed):
                if published and published.isoformat() < since[:10]:
                    continue
                yield url
            return
        try:
            index = self._client.get(CONSULTATIONS).content
        except FetchError:
            return
        yield CONSULTATIONS
        yield from consultation_links(index)


class DMAAnnualReportsAdapter(_DMADocumentsAdapter):
    source = "dma-annual-reports"
    doc_type = DocType.PREPARATORY  # an Article 35 report to Parliament and Council
    tags = ("annual-report",)

    def _index_pages(self, since: str | None) -> Iterator[str]:
        # One page, one report a year. Nothing to paginate or cursor.
        yield ANNUAL_REPORTS
