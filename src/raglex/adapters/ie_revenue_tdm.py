"""Irish Revenue Tax and Duty Manuals from the official live register."""

from __future__ import annotations

import re
from collections import deque
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.revenue.ie"
INDEX = BASE + "/en/tax-professionals/tdm/index.aspx"
_ROOT_PATH = re.compile(r"^/en/tax-professionals/tdm(?:-wm)?/")
_ARCHIVE_STAMP = re.compile(r"-(\d{14})\.pdf$", re.I)


def _archive_date(href: str) -> date | None:
    match = _ARCHIVE_STAMP.search(href)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").date()
    except ValueError:
        return None


def _identity(href: str) -> str:
    path = urlsplit(href).path
    path = _ROOT_PATH.sub("", path).removesuffix(".pdf")
    return "ie/revenue-tdm/" + path.lower()


def parse_tdm_index(raw: bytes | str, page_url: str) -> tuple[list[dict], list[str]]:
    """Return current manuals and child index pages from one Revenue page."""
    soup = BeautifulSoup(raw, "html.parser")
    manuals: list[dict] = []
    for item in soup.select("ul.documents-list > li"):
        current = item.select_one("a.pdf[href$='.pdf']")
        if current is None:
            continue
        href = urljoin(page_url, current.get("href") or "")
        description = item.select_one(".spanDesTDM")
        code = " ".join(current.get_text(" ", strip=True).split())
        desc = (
            " ".join(description.get_text(" ", strip=True).split())
            if description else ""
        )
        archives = [
            urljoin(page_url, a.get("href") or "")
            for a in item.select(".older-versions a[href$='.pdf']")
            if _archive_date(a.get("href") or "")
        ]
        dated = sorted(
            ((d, u) for u in archives if (d := _archive_date(u))), reverse=True
        )
        changed = dated[0][0] if dated else None
        manuals.append({
            "stable_id": _identity(href),
            "title": f"{code} — {desc}" if desc else code,
            "code": code,
            "description": desc,
            "pdf_url": href,
            "index_url": page_url,
            "changed": changed,
            # When Revenue replaces a current PDF, that rendition appears in the
            # timestamped archive list. Its newest timestamp is the cheap update signal.
            "contenthash": dated[0][1] if dated else href,
        })

    children: list[str] = []
    for link in soup.select("a[href]"):
        href = urljoin(page_url, link.get("href") or "")
        path = urlsplit(href).path
        if (
            href.startswith(BASE)
            and _ROOT_PATH.match(path)
            and path.lower().endswith("/index.aspx")
            and href != page_url
        ):
            children.append(href)
    return manuals, list(dict.fromkeys(children))


class IrishRevenueTDMAdapter(BaseAdapter):
    source = "ie-revenue-tdm"
    min_interval = 0.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        queue = deque([INDEX])
        visited: set[str] = set()
        seen_docs: set[str] = set()
        while queue:
            if max_pages is not None and len(visited) >= max_pages:
                return
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            response = self._client.get(url)
            manuals, children = parse_tdm_index(response.content, url)
            queue.extend(child for child in children if child not in visited)
            for row in manuals:
                if row["stable_id"] in seen_docs:
                    continue
                seen_docs.add(row["stable_id"])
                yield Stub(
                    stable_id=row["stable_id"],
                    landing_url=row["index_url"],
                    raw_url=row["pdf_url"],
                    hint_date=row["changed"],
                    title=row["title"],
                    court="Irish Revenue",
                    hints={
                        **row,
                        "watermark": (
                            row["changed"].isoformat() if row["changed"] else None
                        ),
                    },
                )

    def fetch(self, stub: Stub) -> Record | None:
        raw = self._client.get(stub.raw_url).content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        if len(text) < 100:
            return None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=stub.title,
            court="Irish Revenue",
            decision_date=stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["ie", "tax", "revenue", "tax-duty-manual", "regulatory"],
            extra={
                "jurisdiction": "ie",
                "manual_code": stub.hints.get("code"),
                "description": stub.hints.get("description"),
                "download_url": stub.raw_url,
                "contenthash": stub.hints.get("contenthash"),
                "require_recognized_legal_citation": True,
            },
        )
