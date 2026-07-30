"""European Commission consumer-law guidance and CPC coordinated positions.

EUR-Lex covers OJ C notices, but much of the Commission's operational consumer-law
interpretation is published only on ``commission.europa.eu``: CPC common positions,
common understandings, sweeps, coordinated-action commitments and DG JUST guidance.
The Commission sitemap is the stable enumeration surface.  It currently exposes a
small English ``/topics/consumers/`` subtree; each changed page is fetched and its
first-party document UUIDs are emitted as separate, citable records.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Iterator
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://commission.europa.eu"
SITEMAP = f"{BASE}/sitemap.xml"
CONSUMER_PATH = "/topics/consumers/"
_DOC_RE = re.compile(r"/document/(?:download/)?([0-9a-f-]{36})_en(?:[/?#]|$)", re.I)
_TITLE_DIRECTIVE_RE = re.compile(
    r"\bDirective\s*(?:\((?:EU|EC|EEC)\)\s*)?"
    r"(?P<year>\d{2,4})\s*/\s*(?P<number>\d{1,4})(?:\s*/\s*(?:EU|EC|EEC))?\b",
    re.I,
)


def _date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        pass
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def sitemap_consumer_pages(xml: bytes) -> list[tuple[str, str | None]]:
    """English consumer-topic URLs and last-modified cursors from the sitemap."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out: list[tuple[str, str | None]] = []
    for item in root:
        values = {el.tag.rsplit("}", 1)[-1]: (el.text or "").strip() for el in item}
        url = values.get("loc") or ""
        if url.startswith(BASE + CONSUMER_PATH) and url.endswith("_en"):
            out.append((url, values.get("lastmod") or None))
    return sorted(out, key=lambda row: row[1] or "", reverse=True)


def _main_text(soup: BeautifulSoup) -> str:
    main = soup.select_one("main") or soup
    for tag in main.select("script, style, nav, form, .ecl-breadcrumb, .ecl-page-header"):
        tag.decompose()
    lines = [" ".join(line.split()) for line in main.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _page_title(soup: BeautifulSoup) -> str | None:
    node = soup.select_one("h1")
    return " ".join(node.get_text(" ", strip=True).split()) if node else None


def title_default_instrument(title: str | None) -> dict | None:
    """A governing directive only when the document title itself names exactly one."""
    found: list[str] = []
    for match in _TITLE_DIRECTIVE_RE.finditer(title or ""):
        year = int(match.group("year"))
        if year < 100:
            year += 1900
        found.append(f"3{year:04d}L{int(match.group('number')):04d}")
    unique = list(dict.fromkeys(found))
    return {"id": unique[0], "kind": "directive"} if len(unique) == 1 else None


def document_stubs(html: bytes, *, page_url: str, watermark: str | None) -> list[Stub]:
    """First-party document UUIDs linked from a consumer page.

    Direct downloads win over their document landing page because they save a request;
    both forms share the same UUID and therefore one stable identity.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Stub] = {}
    for link in soup.select("a[href]"):
        href = str(link.get("href") or "")
        match = _DOC_RE.search(href)
        if not match:
            continue
        uuid = match.group(1).lower()
        card = link.find_parent(class_="ecl-file")
        title_node = card.select_one(".ecl-file__title") if card else None
        title = (
            title_node.get_text(" ", strip=True) if title_node
            else link.get("data-untranslated-label") or link.get_text(" ", strip=True)
        )
        date_node = card.select_one(".ecl-file__detail-meta-item") if card else None
        published = _date(date_node.get_text(" ", strip=True) if date_node else None)
        absolute = urljoin(BASE, href)
        is_download = "/document/download/" in absolute
        previous = found.get(uuid)
        if previous and not is_download:
            continue
        found[uuid] = Stub(
            stable_id=f"eu/commission/document/{uuid}",
            landing_url=f"{BASE}/document/{uuid}_en",
            raw_url=absolute,
            title=" ".join(str(title or "").split()) or None,
            hint_date=published,
            hints={
                "kind": "document",
                "uuid": uuid,
                "origin_page": page_url,
                "watermark": watermark,
                "contenthash": watermark,
                "direct_download": is_download,
            },
        )
    return list(found.values())


class EUConsumerGuidanceAdapter(BaseAdapter):
    source = "eu-consumer-guidance"
    min_interval = 1.25

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        pages = sitemap_consumer_pages(self._client.get(SITEMAP).content)
        visited = 0
        for page_url, lastmod in pages:
            if since and lastmod and lastmod <= since:
                continue
            if max_pages is not None and visited >= max_pages:
                return
            response = self._client.get(page_url)
            visited += 1
            path = urlparse(page_url).path.strip("/")
            soup = BeautifulSoup(response.content, "html.parser")
            yield Stub(
                stable_id=f"eu/commission/page/{path}",
                landing_url=page_url,
                raw_url=page_url,
                title=_page_title(soup),
                hint_date=_date(lastmod),
                hints={
                    "kind": "page",
                    "watermark": lastmod,
                    "contenthash": lastmod,
                    "html": response.content,
                },
            )
            yield from document_stubs(
                response.content, page_url=page_url, watermark=lastmod
            )

    def fetch(self, stub: Stub) -> Record | None:
        if stub.hints.get("kind") == "page":
            raw = stub.hints.get("html")
            if not isinstance(raw, bytes):
                raw = self._client.get(stub.raw_url).content
            soup = BeautifulSoup(raw, "html.parser")
            text = _main_text(soup)
            if len(text) < 80:
                return None
            return Record(
                source=self.source,
                stable_id=stub.stable_id,
                doc_type=DocType.GUIDANCE,
                title=_page_title(soup) or stub.title,
                court="European Commission",
                decision_date=stub.hint_date,
                language="en",
                source_language="en",
                landing_url=stub.landing_url,
                raw_bytes=raw,
                raw_ext="html",
                text=text,
                extracted_via=ExtractedVia.STRUCTURED,
                topic_tags=["consumer-law", "european-commission", "cpc"],
                extra={
                    "jurisdiction": "eu",
                    "contenthash": stub.hints.get("contenthash"),
                    "commission_surface": "consumer-topic",
                    "citation_default_instrument": title_default_instrument(
                        _page_title(soup) or stub.title
                    ),
                    "require_recognized_legal_citation": False,
                },
            )

        landing_html: bytes | None = None
        download_url = stub.raw_url or ""
        title = stub.title
        published = stub.hint_date
        if not stub.hints.get("direct_download"):
            try:
                landing_html = self._client.get(download_url).content
            except FetchError:
                return None
            soup = BeautifulSoup(landing_html, "html.parser")
            title = _page_title(soup) or title
            link = soup.select_one('a[href*="/document/download/"]')
            if not link:
                return None
            download_url = urljoin(BASE, str(link.get("href") or ""))
            meta = soup.select_one(".ecl-file__detail-meta-item")
            published = _date(meta.get_text(" ", strip=True) if meta else None) or published
        try:
            response = self._client.get(download_url)
        except FetchError:
            return None
        raw = response.content
        mime = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
        suffix = PurePosixPath(urlparse(download_url).path).suffix.lower().lstrip(".")
        ext = "pdf" if raw.startswith(b"%PDF") else (suffix or "bin")
        try:
            extracted = extract_bytes(raw, ext=ext, mime=mime or None)
        except ValueError:
            return None
        text = (extracted.text or "").strip()
        if len(text) < 40:
            return None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=title or stub.stable_id,
            court="European Commission / CPC Network",
            decision_date=published,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=ext,
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["consumer-law", "european-commission", "cpc"],
            extra={
                "jurisdiction": "eu",
                "document_uuid": stub.hints.get("uuid"),
                "download_url": download_url,
                "origin_page": stub.hints.get("origin_page"),
                "contenthash": stub.hints.get("contenthash"),
                "commission_surface": "consumer-document",
                "citation_default_instrument": title_default_instrument(title),
                "require_recognized_legal_citation": False,
            },
        )
