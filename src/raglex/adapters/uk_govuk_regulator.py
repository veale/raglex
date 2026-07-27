"""Legally relevant regulator material from the GOV.UK publishing APIs.

The Search API is the update feed and the Content Store is the canonical structured
body.  Broad organisation feeds intentionally opt into RagLex's legal relevance gate:
all fetched items remain held/deduped, while citation-free operational material is not
embedded, listed or returned by search.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterator
from urllib.parse import urljoin

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.gov.uk"
SEARCH = f"{BASE}/api/search.json"
CONTENT = f"{BASE}/api/content"

_SKIP_TYPES = {
    "finder", "finder_email_signup", "organisation",
    "official_statistics_announcement", "statistics_announcement",
}


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def _html_text(value: str | None) -> str:
    from bs4 import BeautifulSoup

    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "nav"]):
        tag.decompose()
    lines = [" ".join(s.split()) for s in soup.get_text("\n").splitlines()]
    return "\n".join(s for s in lines if s).strip()


def content_text(content: dict) -> str:
    details = content.get("details") or {}
    parts: list[str] = []
    for key in ("body", "hidden_indexable_content", "summary"):
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(_html_text(value))
    for part in details.get("parts") or ():
        if not isinstance(part, dict):
            continue
        heading = str(part.get("title") or "").strip()
        body = _html_text(part.get("body"))
        if body:
            parts.append((f"{heading}\n{body}" if heading else body))
    for doc in details.get("documents") or ():
        if isinstance(doc, str):
            text = _html_text(doc)
            if text:
                parts.append(text)
        elif isinstance(doc, dict):
            text = _html_text(doc.get("body") or doc.get("content"))
            if text:
                parts.append(text)
    description = str(content.get("description") or "").strip()
    if description:
        parts.append(description)
    return "\n\n".join(dict.fromkeys(p for p in parts if p)).strip()


class GOVUKRegulatorAdapter(BaseAdapter):
    min_interval = 0.75
    page_size = 200

    def __init__(
        self,
        *,
        source: str,
        organisation: str | None = None,
        document_type: str | None = None,
        court: str,
        client: RateLimitedClient | None = None,
    ) -> None:
        if not organisation and not document_type:
            raise ValueError("organisation or document_type is required")
        self.source = source
        self.organisation = organisation
        self.document_type = document_type
        self.court = court
        self._client = client or RateLimitedClient(
            source, min_interval=self.min_interval, timeout=60
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        start = pages = 0
        while True:
            params = {
                "count": self.page_size,
                "start": start,
                "order": "-public_timestamp",
                "fields": (
                    "title,link,description,public_timestamp,"
                    "content_store_document_type"
                ),
            }
            if self.organisation:
                params["filter_organisations"] = self.organisation
            if self.document_type:
                params["filter_document_type"] = self.document_type
            data = self._client.get(SEARCH, params=params).json()
            items = data.get("results") or []
            if not items:
                return
            for item in items:
                published = str(item.get("public_timestamp") or "")
                if since and published and published <= since:
                    return
                link = str(item.get("link") or "")
                kind = str(item.get("content_store_document_type") or "")
                if not link.startswith("/") or kind in _SKIP_TYPES:
                    continue
                yield Stub(
                    stable_id=f"{self.source}/{link.strip('/')}",
                    landing_url=urljoin(BASE, link),
                    raw_url=f"{CONTENT}{link}",
                    title=item.get("title"),
                    court=self.court,
                    hint_date=_date(published),
                    hints={
                        "watermark": published,
                        "contenthash": published,
                        "description": item.get("description"),
                        "content_type": kind,
                        "feed_total": data.get("total"),
                    },
                )
            pages += 1
            start += len(items)
            if start >= int(data.get("total") or 0) or len(items) < self.page_size:
                return
            if max_pages is not None and pages >= max_pages:
                return

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url)
        try:
            content = response.json()
        except ValueError:
            return None
        text = content_text(content)
        details = content.get("details") or {}
        attachment_meta: list[dict] = []
        # Decisions are often a short landing page plus the legally operative PDF.
        # Include each English PDF before applying the citation gate.
        for attachment in details.get("attachments") or ():
            if not isinstance(attachment, dict):
                continue
            url = str(attachment.get("url") or "")
            mime = str(attachment.get("content_type") or "")
            if not url or ("pdf" not in mime.lower() and not url.lower().endswith(".pdf")):
                continue
            try:
                pdf = self._client.get(url).content
                extracted = extract_bytes(pdf, ext="pdf", mime="application/pdf")
            except (FetchError, ValueError):
                continue
            body = (extracted.text or "").strip()
            if body:
                text += "\n\n" + body
            attachment_meta.append({
                "url": url, "title": attachment.get("title"), "bytes": len(pdf),
                "text_chars": len(body),
            })
        text = text.strip()
        if len(text) < 40:
            return None
        base_path = str(content.get("base_path") or "")
        updated = content.get("public_updated_at") or stub.hints.get("watermark")
        first = content.get("first_published_at")
        doc_type = (
            DocType.DECISION
            if self.document_type == "cma_case"
            else DocType.GUIDANCE
        )
        return Record(
            source=self.source,
            stable_id=f"{self.source}/{base_path.strip('/') or stub.stable_id.split('/', 1)[-1]}",
            doc_type=doc_type,
            title=content.get("title") or stub.title,
            court=self.court,
            decision_date=_date(first) or stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=response.content,
            raw_ext="json",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["regulatory", self.court.lower()],
            extra={
                "jurisdiction": "gb",
                "content_id": content.get("content_id"),
                "content_type": content.get("document_type") or stub.hints.get("content_type"),
                "first_published_at": first,
                "updated_at": updated,
                "contenthash": stub.hints.get("contenthash"),
                "attachments": attachment_meta,
                "require_recognized_legal_citation": True,
            },
        )

