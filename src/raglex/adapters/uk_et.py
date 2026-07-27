"""UK Employment Tribunal decisions — GOV.UK Search + Content Store APIs.

GOV.UK publishes the complete Employment Tribunal register through two ordinary JSON
endpoints:

* ``/api/search.json?filter_format=employment_tribunal_decision`` discovers decisions,
  newest publication first;
* ``/api/content/{path}`` carries the full PDF-derived text in
  ``details.metadata.hidden_indexable_content``.

The important RagLex-specific detail is identity.  The corpus already contains many
BAILII metadata stubs keyed like ``uket/2023/3314122_2020``.  GOV.UK uses opaque content
UUIDs, but every title and judgment body carries the tribunal case number.  Keying by
decision year + case number enriches those existing nodes instead of creating a second
copy under the UUID.  Additional case numbers on a joined decision become aliases.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE_URL = "https://www.gov.uk"
SEARCH_URL = f"{BASE_URL}/api/search.json"
CONTENT_URL = f"{BASE_URL}/api/content"

_CASE_NUMBER_RE = re.compile(r"\b(?P<number>\d{5,8})\s*/\s*(?P<year>(?:19|20)\d{2})\b")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t]+")


def case_numbers(value: str | None) -> list[str]:
    """Distinct ET case numbers (``6031054/2025``) in source order."""
    return list(dict.fromkeys(
        f"{m.group('number')}/{m.group('year')}" for m in _CASE_NUMBER_RE.finditer(value or "")
    ))


def uket_id(number: str, decision_date: date) -> str:
    """The citation-compatible identity used by the existing BAILII seed."""
    return f"uket/{decision_date.year}/{number.replace('/', '_')}"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _plain_html(value: str | None) -> str:
    """Small dependency-free fallback for the Content Store's short body HTML."""
    text = _TAG_RE.sub(" ", value or "")
    return _SPACE_RE.sub(" ", html.unescape(text)).strip()


def parse_search_page(raw: bytes | str) -> tuple[int, list[dict]]:
    """Pure parser for one GOV.UK Search API page."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return 0, []
    results = data.get("results")
    return int(data.get("total") or 0), results if isinstance(results, list) else []


class UKEmploymentTribunalAdapter(BaseAdapter):
    source = "uk-et"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    page_size = 500

    def __init__(
        self,
        *,
        client: RateLimitedClient | None = None,
        query: str | None = None,
    ) -> None:
        self.query = (query or "").strip() or None
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=60
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        start = 0
        page = 0
        while True:
            params = {
                "filter_format": "employment_tribunal_decision",
                "count": self.page_size,
                "start": start,
                "order": "-public_timestamp",
                "fields": (
                    "title,link,public_timestamp,tribunal_decision_decision_date,"
                    "tribunal_decision_categories,tribunal_decision_country"
                ),
            }
            if self.query:
                params["q"] = self.query
            response = self._client.get(SEARCH_URL, params=params)
            total, items = parse_search_page(response.content)
            if not items:
                return

            for item in items:
                published = str(item.get("public_timestamp") or "")
                # Search is newest-publication first, so this is a safe early stop.
                if since and published and published <= since:
                    return
                link = str(item.get("link") or "")
                title = str(item.get("title") or "")
                decision_date = _parse_date(item.get("tribunal_decision_decision_date"))
                numbers = case_numbers(title)
                provisional = (
                    uket_id(numbers[0], decision_date)
                    if numbers and decision_date
                    else f"uk-et/{link.rstrip('/').rsplit('/', 1)[-1]}"
                )
                yield Stub(
                    stable_id=provisional,
                    landing_url=urljoin(BASE_URL, link),
                    raw_url=f"{CONTENT_URL}{link}",
                    hint_date=decision_date,
                    title=title or None,
                    court="uket",
                    hints={
                        "watermark": published or None,
                        "published": published or None,
                        "categories": item.get("tribunal_decision_categories") or [],
                        "country": item.get("tribunal_decision_country"),
                        "case_numbers": numbers,
                        "feed_total": total,
                    },
                )

            page += 1
            start += len(items)
            if start >= total or len(items) < self.page_size:
                return
            if max_pages is not None and page >= max_pages:
                return

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url)
        raw = response.content
        try:
            content = response.json()
        except (TypeError, ValueError):
            return None

        details = content.get("details") or {}
        metadata = details.get("metadata") or {}
        decision_date = (
            _parse_date(metadata.get("tribunal_decision_decision_date"))
            or stub.hint_date
            or _parse_date(content.get("first_published_at"))
        )
        if decision_date is None:
            return None

        title = str(content.get("title") or stub.title or "").strip()
        numbers = case_numbers(title)
        if not numbers:
            numbers = list(stub.hints.get("case_numbers") or ())

        text = str(metadata.get("hidden_indexable_content") or "").strip()
        raw_ext = "json"
        # Most records already contain PDF-derived text.  Older outliers sometimes carry
        # only an attachment: fetch the official PDF rather than storing another stub.
        if len(text) < 50:
            for attachment in details.get("attachments") or ():
                url = str(attachment.get("url") or "")
                mime = str(attachment.get("content_type") or "")
                if not url or ("pdf" not in mime.lower() and not url.lower().endswith(".pdf")):
                    continue
                try:
                    pdf = self._client.get(url).content
                    extracted = extract_bytes(pdf, ext="pdf", mime="application/pdf")
                except (FetchError, ValueError):
                    continue
                if (extracted.text or "").strip():
                    raw, raw_ext, text = pdf, "pdf", extracted.text.strip()
                    break
        if len(text) < 50:
            text = _plain_html(details.get("body"))
        if len(text) < 50:
            return None

        content_id = str(content.get("content_id") or "")
        stable_id = (
            uket_id(numbers[0], decision_date)
            if numbers
            else f"uk-et/{content_id or stub.stable_id.rsplit('/', 1)[-1]}"
        )
        aliases = [uket_id(n, decision_date) for n in numbers[1:]]

        categories = metadata.get("tribunal_decision_categories")
        if not isinstance(categories, list):
            categories = list(stub.hints.get("categories") or ())
        country = metadata.get("tribunal_decision_country") or stub.hints.get("country")

        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.JUDGMENT,
            title=title or stable_id,
            court="uket",
            decision_date=decision_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=raw_ext,
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["uk-caselaw", "employment-tribunal"],
            extra={
                "jurisdiction": "gb",
                "content_id": content_id or None,
                "case_numbers": numbers,
                "neutral_citation": (
                    f"[{decision_date.year}] UKET {numbers[0]}" if numbers else None
                ),
                "categories": categories,
                "country": country,
                "published_at": content.get("first_published_at") or stub.hints.get("published"),
                "updated_at": content.get("public_updated_at"),
                "aliases": aliases,
            },
        )
