"""Rijksinspectie Digitale Infrastructuur (RDI) document register.

The public RSS feed is intentionally only a recent-items feed (20 entries, with no
pagination element).  It is useful to humans but cannot backfill the 363-result archive.
The same site's JSON search endpoint reports ``total_pages`` and accepts ``current``;
that bounded, newest-first interface drives both backfill and watch so a busy month can
never overflow an RSS window.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction.ocr import text_or_ocr

BASE = "https://www.rdi.nl"
SEARCH = f"{BASE}/api/search"
PAGE_SIZE = 10

_FIELDS = {
    "url": {"raw": {}}, "page_title": {"raw": {}},
    "meta_description": {"raw": {}}, "sort_date": {"raw": {}},
    "information_type": {"raw": {}},
}


def search_payload(page: int) -> dict:
    filter_ = {"field": "content_type", "values": ["pro:downloadDocument"], "type": "any"}
    return {
        "requestState": {"current": page, "filters": [filter_], "resultsPerPage": PAGE_SIZE,
                         "searchTerm": "", "sortDirection": "", "sortField": "", "sortList": []},
        "queryConfig": {"filters": [filter_], "result_fields": _FIELDS, "facets": {},
                        "sortList": [{"field": "sort_date", "direction": "desc"}]},
    }


def _raw(row: dict, key: str) -> str:
    value = row.get(key) or {}
    return str(value.get("raw") or "") if isinstance(value, dict) else str(value or "")


def _day(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).date()
    except ValueError:
        return None


def rdi_search_page(payload: dict) -> tuple[list[Stub], int, int]:
    info = payload.get("info", {}).get("meta", {}).get("page", {})
    total_pages = option_int(payload.get("totalPages", info.get("total_pages")), 0)
    total_results = option_int(payload.get("totalResults", info.get("total_results")), 0)
    if total_pages <= 0:
        raise FetchError("nl-rdi: search response had no page count")
    rows = payload.get("results") or payload.get("rawResponse", {}).get("rawResults") or []
    out = []
    for row in rows:
        row = row.get("data", row)
        document_id = _raw(row, "id") or str(row.get("_meta", {}).get("id") or "")
        landing = urljoin(BASE, _raw(row, "url"))
        if not document_id or "/documenten/" not in landing:
            continue
        changed = _day(_raw(row, "sort_date"))
        out.append(Stub(
            stable_id=f"nl/rdi/{document_id}", landing_url=landing,
            title=_raw(row, "page_title"), hint_date=changed, court="rdi",
            hints={"document_id": document_id, "information_type": _raw(row, "information_type"),
                   "summary": _raw(row, "meta_description"),
                   "watermark": changed.isoformat() if changed else None},
        ))
    return out, total_pages, total_results


def rdi_detail(html: bytes | str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    elastic = soup.select_one("script#elastic-content")
    metadata = {}
    if elastic:
        try:
            metadata = json.loads(elastic.string or "{}")
        except json.JSONDecodeError:
            pass
    files = []
    for link in soup.select("a.download-list__link[href]"):
        url = urljoin(BASE, str(link.get("href") or ""))
        file_title_node = link.select_one(".title")
        file_title = " ".join(file_title_node.get_text(" ", strip=True).split()) if file_title_node else ""
        if urlsplit(url).path.lower().endswith(".pdf") and not any(item["url"] == url for item in files):
            files.append({"url": url, "title": file_title})
    title_node = soup.select_one("h1")
    return {
        "title": str(metadata.get("pageTitle") or
                     (title_node.get_text(" ", strip=True) if title_node else "")),
        "information_type": str((metadata.get("informationType") or {}).get("label") or ""),
        "publication_date": _day(str(metadata.get("publicationDate") or "")),
        "files": files,
    }


def rdi_doc_type(title: str, information_type: str) -> DocType:
    value = f"{title} {information_type}".casefold()
    if re.search(r"\b(beschikking|boetebesluit|besluit)\b", value):
        return DocType.DECISION
    if re.search(r"\b(advies|zienswijze)\b", value):
        return DocType.OPINION
    if information_type.casefold() == "regeling":
        return DocType.LEGISLATION
    return DocType.GUIDANCE


class RDIDocumentsAdapter(BaseAdapter):
    source = "nl-rdi"
    min_interval = 0.8

    def __init__(self, *, client: RateLimitedClient | None = None,
                 start_offset: int | str | None = None):
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval, timeout=120)
        self.start_offset = resume_floor(option_int(start_offset, 0), PAGE_SIZE)

    def _page(self, page: int) -> dict:
        response = self._client.request(
            "POST", SEARCH, json=search_payload(page),
            headers={"Accept": "application/json", "Origin": BASE,
                     "Referer": f"{BASE}/documenten"})
        try:
            return response.json()
        except (ValueError, AttributeError) as exc:
            raise FetchError(f"{self.source}: search returned non-JSON content") from exc

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # RDI numbers pages from 1. start_offset counts the entire sorted feed.
        first_page = self.start_offset // PAGE_SIZE + 1
        first = self._page(first_page)
        rows, total_pages, total_results = rdi_search_page(first)
        last = total_pages
        if max_pages is not None:
            last = min(last, first_page + max_pages - 1)
        for page in range(first_page, last + 1):
            if page != first_page:
                rows, current_total, current_results = rdi_search_page(self._page(page))
                if current_total != total_pages or current_results != total_results:
                    # A newly-published item can move page boundaries during a long run.
                    # Backing off on resume is safe; silently accepting a shrinking feed is not.
                    total_pages, total_results = current_total, current_results
            if not rows and (page - 1) * PAGE_SIZE < total_results:
                raise FetchError(f"{self.source}: page {page} was empty inside the register")
            for parent in rows:
                detail = rdi_detail(self._client.get(parent.landing_url).content)
                files = detail["files"]
                for index, file_info in enumerate(files):
                    raw_url = file_info["url"]
                    suffix = "" if len(files) == 1 else f"/{index + 1}-{urlsplit(raw_url).path.rsplit('/', 1)[-1]}"
                    yield Stub(
                        stable_id=parent.stable_id + suffix, landing_url=parent.landing_url,
                        raw_url=raw_url, title=file_info["title"] or detail["title"] or parent.title,
                        hint_date=detail["publication_date"] or parent.hint_date, court="rdi",
                        hints={**parent.hints,
                               "information_type": detail["information_type"] or parent.hints.get("information_type"),
                               "feed_total": total_results,
                               "resume_offset": (page - 1) * PAGE_SIZE},
                    )

    def fetch(self, stub: Stub) -> Record | None:
        blob = self._client.get(stub.raw_url).content
        if not blob.startswith(b"%PDF"):
            raise FetchError(f"{self.source}: advertised download was not a PDF")
        text, needs, spans, engine = text_or_ocr(blob, max_pages=160)
        if len(text) < 120:
            return None
        information_type = str(stub.hints.get("information_type") or "Publicatie")
        return Record(
            source=self.source, stable_id=stub.stable_id,
            doc_type=rdi_doc_type(stub.title or "", information_type),
            title=stub.title, court="rdi", decision_date=stub.hint_date,
            language="nl", source_language="nl", landing_url=stub.landing_url,
            raw_bytes=blob, raw_ext="pdf", text=text, extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["netherlands", "rdi", information_type.casefold().replace(" ", "-")],
            extra={"jurisdiction": "nl", "publisher": "Rijksinspectie Digitale Infrastructuur",
                   "information_type": information_type, "summary": stub.hints.get("summary"),
                   "needs_ocr": needs, "page_spans": spans, "extraction_engine": engine,
                   "citation_languages": ["nl"]},
        )
