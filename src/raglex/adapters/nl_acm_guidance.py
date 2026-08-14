"""Dutch ACM guidance (``Leidraden``) for businesses and consumer enforcement."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_flag, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes
from ..extraction.ocr import text_or_ocr

BASE = "https://www.acm.nl"
LISTING = f"{BASE}/nl/publicaties/voorlichting-aan-bedrijven/acm-leidraad"
SEARCH_AJAX = f"{BASE}/nl/views/ajax"
LEGAL_RSS = f"{BASE}/nl/nieuws/rss/publicaties"
LEGAL_TYPES = ("Beslissing op bezwaar", "Besluit", "Gerechtelijke uitspraak", "Visie en opinie")
LEGAL_PAGE_SIZE = 10
_ECLI = re.compile(r"\bECLI:NL:[A-Z0-9]+:\d{4}:\d+\b", re.I)


def _date(value: str | None) -> date | None:
    value = (value or "").strip()
    for fmt in ("%d-%m-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def guidance_stubs(html: bytes) -> list[Stub]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Stub] = []
    seen: set[str] = set()
    for link in soup.select('a[href^="/nl/publicaties/"]'):
        href = str(link.get("href") or "")
        if href in seen or href.rstrip("/") == urlparse(LISTING).path:
            continue
        card = link.find_parent(class_=re.compile(r"(?:card|views-row)"))
        if not card:
            continue
        title = " ".join(link.get_text(" ", strip=True).split())
        if not title:
            continue
        seen.add(href)
        card_text = card.get_text(" ", strip=True)
        match = re.search(r"\b(\d{2}-\d{2}-\d{4})\b", card_text)
        published = _date(match.group(1) if match else None)
        out.append(Stub(
            stable_id=f"nl/acm/{href.strip('/').rsplit('/', 1)[-1]}",
            landing_url=urljoin(BASE, href),
            raw_url=urljoin(BASE, href),
            title=title,
            court="ACM",
            hint_date=published,
            hints={"watermark": published.isoformat() if published else None},
        ))
    return out


def _main_text(soup: BeautifulSoup) -> str:
    main = soup.select_one("main#main-content") or soup.select_one("main") or soup
    for tag in main.select(
        "script, style, nav, form, .m-breadcrumb, .m-back-to-top, "
        ".block-related-content, .views-element-container"
    ):
        tag.decompose()
    lines = [" ".join(line.split()) for line in main.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line).strip()


class ACMGuidanceAdapter(BaseAdapter):
    source = "nl-acm-guidance"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None,
                 start_offset: int | str | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90
        )
        # This source itself does not checkpoint, but it shares a module with the
        # resumable legal register; accepting the orchestrator keyword keeps the
        # registry-wide source/module contract unambiguous.
        self.start_offset = resume_floor(option_int(start_offset, 0), 1)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # ACM sometimes updates an old guide without changing its original publication
        # date. The catalogue is only three pages, so walk it all and let payload hashes
        # detect revisions rather than using an unsafe date early-stop.
        page = 0
        seen: set[str] = set()
        while True:
            response = self._client.get(LISTING, params={"page": page})
            rows = guidance_stubs(response.content)
            fresh = [row for row in rows if row.stable_id not in seen]
            if not fresh:
                return
            for stub in fresh:
                seen.add(stub.stable_id)
                yield stub
            page += 1
            if max_pages is not None and page >= max_pages:
                return
            soup = BeautifulSoup(response.content, "html.parser")
            if not soup.select_one('a[rel="next"], .m-pager__next'):
                return

    def fetch(self, stub: Stub) -> Record | None:
        try:
            response = self._client.get(stub.raw_url)
        except FetchError:
            return None
        soup = BeautifulSoup(response.content, "html.parser")
        text = _main_text(soup)
        attachments: list[dict] = []
        for link in soup.select('main a[href$=".pdf"], main a[href*=".pdf?"]'):
            url = urljoin(BASE, str(link.get("href") or ""))
            if any(row["url"] == url for row in attachments):
                continue
            try:
                pdf_response = self._client.get(url)
            except FetchError:
                continue
            raw = pdf_response.content
            if not raw.startswith(b"%PDF"):
                continue
            extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
            body = (extracted.text or "").strip()
            if body:
                text += "\n\n" + body
            attachments.append({
                "url": url,
                "title": " ".join(link.get_text(" ", strip=True).split()) or None,
                "bytes": len(raw),
                "text_chars": len(body),
            })
        date_node = next(
            (node for node in soup.find_all(string=re.compile(r"^\d{2}-\d{2}-\d{4}$"))),
            None,
        )
        published = _date(str(date_node)) if date_node else stub.hint_date
        if len(text.strip()) < 80:
            return None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=(soup.select_one("h1").get_text(" ", strip=True)
                   if soup.select_one("h1") else stub.title),
            court="Autoriteit Consument & Markt",
            decision_date=published,
            language="nl",
            source_language="nl",
            landing_url=stub.landing_url,
            raw_bytes=response.content,
            raw_ext="html",
            text=text.strip(),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["consumer-law", "competition-law", "acm", "netherlands"],
            extra={
                "jurisdiction": "nl",
                "attachments": attachments,
                "require_recognized_legal_citation": False,
            },
        )


def _legal_params(page: int) -> dict:
    params = {
        "view_name": "search_v2", "view_display_id": "page", "view_args": "",
        "view_path": "/zoeken", "view_base_path": "zoeken", "view_dom_id": "raglex",
        "pager_element": 0, "keyword": "All", "mixed_content_type[publication]": "publication",
        "sort_by": "created", "text": "", "page": page, "_drupal_ajax": 1,
        "_wrapper_format": "drupal_ajax",
    }
    for publication_type in LEGAL_TYPES:
        params[f"field_publication_type_name[{publication_type}]"] = publication_type
    return params


def legal_ajax_html(payload: list[dict]) -> str:
    for command in payload:
        if (command.get("command") == "insert" and
                "js-view-dom-id" in str(command.get("selector") or "") and
                command.get("data")):
            return str(command["data"])
    raise FetchError("nl-acm-publications: Drupal response contained no result view")


def legal_stubs(html: bytes | str) -> tuple[list[Stub], int]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select(".m-card"):
        link = card.select_one(".m-card__title a[href]")
        meta = [_clean.get_text(" ", strip=True) for _clean in card.select(".m-card__meta span")]
        if not link or len(meta) < 3 or meta[1] not in LEGAL_TYPES:
            continue
        href = str(link.get("href") or "")
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        published = _date(meta[-1])
        summary_node = card.select_one(".m-card__body")
        out.append(Stub(
            stable_id=f"nl/acm/publication/{slug}", landing_url=urljoin(BASE, href),
            title=" ".join(link.get_text(" ", strip=True).split()), court="ACM",
            hint_date=published,
            hints={"publication_type": meta[1],
                   "summary": " ".join(summary_node.get_text(" ", strip=True).split()) if summary_node else None,
                   "watermark": published.isoformat() if published else None},
        ))
    last = soup.select_one(".m-pager__item--last a[href]")
    match = re.search(r"(?:[?&]|&amp;)page=(\d+)", str(last.get("href") or "") if last else "")
    total_pages = int(match.group(1)) + 1 if match else (1 if out else 0)
    return out, total_pages


def legal_rss_stubs(xml: bytes | str) -> list[Stub]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FetchError("nl-acm-publications: RSS returned invalid XML") from exc
    out = []
    for item in root.findall("./channel/item"):
        landing = (item.findtext("link") or "").strip()
        if "/nl/publicaties/" not in landing:
            continue
        slug = landing.rstrip("/").rsplit("/", 1)[-1]
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").date()
        except (TypeError, ValueError):
            published = None
        out.append(Stub(
            stable_id=f"nl/acm/publication/{slug}", landing_url=landing,
            title=(item.findtext("title") or "").strip(), court="ACM", hint_date=published,
            hints={"summary": (item.findtext("description") or "").strip(),
                   "watermark": published.isoformat() if published else None},
        ))
    if not out:
        raise FetchError("nl-acm-publications: RSS contained no publications")
    return out


def legal_detail(html: bytes | str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("h1")
    type_node = soup.select_one('meta[name="dcterms.type"]')
    publication_type = str(type_node.get("content") or "") if type_node else ""
    files = []
    for link in soup.select('main a[href*=".pdf"]'):
        url = urljoin(BASE, str(link.get("href") or "").split("?", 1)[0])
        if not url.lower().endswith(".pdf") or any(row["url"] == url for row in files):
            continue
        label = re.sub(r"\s*\(PDF\s*-.*$", "", " ".join(link.get_text(" ", strip=True).split()), flags=re.I)
        files.append({"url": url, "title": label})
    eclis = []
    for link in soup.select('main a[href*="ECLI:"]'):
        match = _ECLI.search(str(link.get("href") or "") + " " + link.get_text(" ", strip=True))
        if match and match.group(0).upper() not in eclis:
            eclis.append(match.group(0).upper())
    body_soup = BeautifulSoup(html, "html.parser")
    body = _main_text(body_soup)
    return {"title": title_node.get_text(" ", strip=True) if title_node else "",
            "publication_type": publication_type, "files": files, "eclis": eclis,
            "body": body}


def _legal_doc_type(publication_type: str) -> DocType:
    return {"Gerechtelijke uitspraak": DocType.JUDGMENT,
            "Visie en opinie": DocType.OPINION}.get(publication_type, DocType.DECISION)


def _court_from_ecli(ecli: str | None) -> str:
    token = (ecli or "").split(":")[2:3]
    return {"CBB": "College van Beroep voor het bedrijfsleven",
            "RBROT": "Rechtbank Rotterdam",
            "HR": "Hoge Raad"}.get(token[0] if token else "", "Nederlandse rechter")


class ACMLegalPublicationsAdapter(BaseAdapter):
    """ACM decisions, objection decisions, opinions, and court publications."""

    source = "nl-acm-publications"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None,
                 start_offset: int | str | None = None, watch_mode=None):
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval, timeout=120)
        self.start_offset = resume_floor(option_int(start_offset, 0), LEGAL_PAGE_SIZE)
        self.watch_mode = option_flag(watch_mode, False)

    def _ajax_page(self, page: int) -> str:
        response = self._client.get(
            SEARCH_AJAX, params=_legal_params(page),
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                     "Referer": f"{BASE}/"})
        try:
            return legal_ajax_html(response.json())
        except (ValueError, AttributeError) as exc:
            raise FetchError(f"{self.source}: Drupal endpoint returned non-JSON content") from exc

    def _parents(self, since: str | None, max_pages: int | None) -> Iterator[Stub]:
        if self.watch_mode or since:
            params = {f"publication_type[{index}]": value
                      for index, value in enumerate((2, 9772, 4, 5))}
            yield from legal_rss_stubs(self._client.get(LEGAL_RSS, params=params).content)
            return
        start_page = self.start_offset // LEGAL_PAGE_SIZE
        first = self._ajax_page(start_page)
        rows, total_pages = legal_stubs(first)
        if total_pages <= 0:
            raise FetchError(f"{self.source}: search returned no pages")
        last = total_pages - 1
        if max_pages is not None:
            last = min(last, start_page + max_pages - 1)
        for page in range(start_page, last + 1):
            if page != start_page:
                rows, _ = legal_stubs(self._ajax_page(page))
            if not rows:
                raise FetchError(f"{self.source}: page {page} was empty inside the register")
            for stub in rows:
                stub.hints["resume_offset"] = page * LEGAL_PAGE_SIZE
                stub.hints["feed_total_pages"] = total_pages
                yield stub

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        for parent in self._parents(since, max_pages):
            response = self._client.get(parent.landing_url)
            detail = legal_detail(response.content)
            publication_type = detail["publication_type"] or parent.hints.get("publication_type")
            files = detail["files"]
            common = {**parent.hints, "publication_type": publication_type,
                      "eclis": detail["eclis"], "html_body": detail["body"]}
            if not files:
                yield Stub(stable_id=parent.stable_id, landing_url=parent.landing_url,
                           raw_url=parent.landing_url, title=detail["title"] or parent.title,
                           hint_date=parent.hint_date, court=parent.court,
                           hints={**common, "html_record": True})
                continue
            for index, file_info in enumerate(files):
                suffix = "" if len(files) == 1 else f"/{index + 1}-{urlparse(file_info['url']).path.rsplit('/', 1)[-1]}"
                yield Stub(stable_id=parent.stable_id + suffix, landing_url=parent.landing_url,
                           raw_url=file_info["url"], title=file_info["title"] or detail["title"] or parent.title,
                           hint_date=parent.hint_date, court=parent.court, hints=common)

    def fetch(self, stub: Stub) -> Record | None:
        publication_type = str(stub.hints.get("publication_type") or "Besluit")
        eclis = list(stub.hints.get("eclis") or [])
        ecli = eclis[0] if len(eclis) == 1 else None
        if stub.hints.get("html_record"):
            response = self._client.get(stub.raw_url)
            text = str(stub.hints.get("html_body") or "").strip()
            blob, ext, needs, spans, engine = response.content, "html", False, [], "html"
        else:
            blob = self._client.get(stub.raw_url).content
            if not blob.startswith(b"%PDF"):
                raise FetchError(f"{self.source}: advertised attachment was not a PDF")
            text, needs, spans, engine = text_or_ocr(blob, max_pages=160)
            ext = "pdf"
            found = _ECLI.findall(text[:30000])
            if len(set(x.upper() for x in found)) == 1:
                ecli = found[0].upper()
        if len(text) < 100:
            return None
        doc_type = _legal_doc_type(publication_type)
        court = _court_from_ecli(ecli) if doc_type == DocType.JUDGMENT else "Autoriteit Consument & Markt"
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=doc_type,
            title=stub.title, court=court, decision_date=stub.hint_date,
            language="nl", source_language="nl", ecli=ecli, landing_url=stub.landing_url,
            raw_bytes=blob, raw_ext=ext, text=text, extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["netherlands", "acm", publication_type.casefold().replace(" ", "-")],
            extra={"jurisdiction": "nl", "publisher": "Autoriteit Consument & Markt",
                   "publication_type": publication_type, "summary": stub.hints.get("summary"),
                   "related_eclis": eclis, "needs_ocr": needs, "page_spans": spans,
                   "extraction_engine": engine, "citation_languages": ["nl"]},
        )
