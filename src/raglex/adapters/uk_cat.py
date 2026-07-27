"""Competition Appeal Tribunal judgments from the Tribunal's official sitemap/PDFs."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.catribunal.org.uk"
SITEMAP = f"{BASE}/sitemap.xml"
_CITE = re.compile(r"\[((?:19|20)\d{2})\]\s*CAT\s*(\d+)", re.I)


def cat_slug(text: str | None) -> str | None:
    m = _CITE.search(text or "")
    return f"cat/{m.group(1)}/{int(m.group(2))}" if m else None


def parse_cat_sitemap(raw: bytes | str) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, TypeError):
        return []
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    out = []
    for row in root.findall(f"{ns}url"):
        loc = (row.findtext(f"{ns}loc") or "").strip()
        if "/judgments/" not in loc:
            continue
        out.append({
            "url": loc,
            "lastmod": (row.findtext(f"{ns}lastmod") or "").strip() or None,
        })
    return out


def parse_cat_page(raw: bytes | str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    h1 = soup.find("h1")
    title = " ".join(h1.get_text(" ", strip=True).split()) if h1 else None
    page_text = " ".join(soup.get_text(" ", strip=True).split())
    cite = _CITE.search(page_text)
    time = soup.find("time")
    when = time.get("datetime") if time else None
    pdf = None
    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        label = link.get_text(" ", strip=True).lower()
        if ("/sites/cat/files/" in href and ".pdf" in href.lower()) or (
            href.lower().endswith(".pdf") and
            ("download" in label or "judgment" in label or "decision" in label)
        ):
            pdf = urljoin(BASE, href)
            break
    return {
        "title": title, "neutral": cite.group(0) if cite else None,
        "date": when, "pdf": pdf, "page_text": page_text,
    }


def _date(value: str | None) -> date | None:
    value = (value or "").strip()
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        pass
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


class CompetitionAppealTribunalAdapter(BaseAdapter):
    source = "uk-cat"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=60
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # The CAT exposes one sitemap rather than paginated result pages.  Treat a
        # requested page as 50 judgment records so a routine one-page watch remains
        # bounded instead of unexpectedly fetching the tribunal's full history.
        limit = max_pages * 50 if max_pages is not None else None
        emitted = 0
        rows = parse_cat_sitemap(self._client.get(SITEMAP).content)
        # Drupal's sitemap is not newest-first.  Sort it before applying the bounded
        # watch limit or a one-page run would repeatedly inspect old judgments.
        rows.sort(key=lambda row: row["lastmod"] or "", reverse=True)
        for row in rows:
            modified = row["lastmod"]
            if since and modified and modified <= since:
                continue
            if limit is not None and emitted >= limit:
                return
            slug = urlsplit(row["url"]).path.rstrip("/").rsplit("/", 1)[-1]
            yield Stub(
                stable_id=f"cat/site/{slug}",
                landing_url=row["url"],
                raw_url=row["url"],
                court="cat",
                hints={
                    "watermark": modified,
                    "contenthash": modified,
                },
            )
            emitted += 1

    def fetch(self, stub: Stub) -> Record | None:
        page = self._client.get(stub.raw_url)
        parsed = parse_cat_page(page.content)
        if not parsed["pdf"]:
            return None
        try:
            pdf = self._client.get(parsed["pdf"]).content
            extracted = extract_bytes(pdf, ext="pdf", mime="application/pdf")
        except (FetchError, ValueError):
            return None
        text = (extracted.text or "").strip()
        stable_id = cat_slug(parsed["neutral"]) or cat_slug(text) or stub.stable_id
        if len(text) < 100:
            return None
        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.JUDGMENT,
            title=parsed["title"] or stable_id,
            court="cat",
            decision_date=_date(parsed["date"]),
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=pdf,
            raw_ext="pdf",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["uk-caselaw", "competition"],
            extra={
                "jurisdiction": "gb",
                "neutral_citation": parsed["neutral"],
                "pdf_url": parsed["pdf"],
                "contenthash": stub.hints.get("contenthash"),
            },
        )
