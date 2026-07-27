"""European Data Protection Supervisor opinions from the official publication list."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)
from ..extraction import extract_bytes

BASE = "https://www.edps.europa.eu"
OPINIONS = BASE + "/data-protection/our-work/our-work-by-type/opinions_en"
EUDPR = "32018R1725"


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        try:
            return datetime.strptime(value or "", "%d %b %Y").date()
        except ValueError:
            return None


def parse_edps_page(raw: bytes | str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    out: list[dict] = []
    for article in soup.select("article.node--type-edpsweb-publication"):
        title_link = article.select_one("h3.node__title a[href]")
        pdf = article.select_one("a[href$='.pdf']")
        if title_link is None or pdf is None:
            continue
        date_node = article.select_one(".edpsweb-publication-date")
        published = " ".join(date_node.get_text(" ", strip=True).split()) if date_node else ""
        title = " ".join(title_link.get_text(" ", strip=True).split())
        landing = urljoin(BASE, title_link.get("href") or "")
        pdf_url = urljoin(BASE, pdf.get("href") or "")
        slug = urlsplit(landing).path.rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            continue
        out.append({
            "stable_id": f"eu/edps/opinion/{slug}",
            "title": title,
            "landing_url": landing,
            "pdf_url": pdf_url,
            "published": _date(published),
        })
    return out


class EDPSOpinionsAdapter(BaseAdapter):
    source = "eu-edps-opinions"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None, fetcher=None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120
        )
        if fetcher is None:
            from ..scraping.fetcher import get_fetcher

            fetcher = get_fetcher(
                "stealth", source=self.source, min_interval=self.min_interval,
                requires_js=True,
            )
        self._fetcher = fetcher

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        page = 0
        while True:
            url = OPINIONS + (f"?page={page}" if page else "")
            fetched = self._fetcher.fetch(url)
            rows = parse_edps_page(fetched.html)
            if not rows:
                return
            stop = False
            for row in rows:
                published = row["published"]
                watermark = published.isoformat() if published else None
                if since and watermark and watermark <= since:
                    stop = True
                    continue
                yield Stub(
                    stable_id=row["stable_id"],
                    landing_url=row["landing_url"],
                    raw_url=row["pdf_url"],
                    hint_date=published,
                    title=row["title"],
                    court="EDPS",
                    hints={
                        "watermark": watermark,
                        "contenthash": row["pdf_url"],
                    },
                )
            page += 1
            if stop or (max_pages is not None and page >= max_pages):
                return

    def fetch(self, stub: Stub) -> Record | None:
        raw = self._client.get(stub.raw_url).content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        if len(text) < 100:
            return None
        number = re.search(r"\bOpinion\s+(\d+/\d{4})\b", stub.title or "", re.I)
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=stub.title,
            court="EDPS",
            decision_date=stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            relations=[TypedRelation(
                relationship_type=RelationshipType.RELATED_TO,
                raw_citation_string="Regulation (EU) 2018/1725, Article 42",
                dst_id=EUDPR,
                dst_anchor="Article 42",
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.RESOLVED,
            )],
            topic_tags=["eu", "edps", "opinions", "regulatory"],
            extra={
                "jurisdiction": "eu",
                "opinion_number": number.group(1) if number else None,
                "regime": EUDPR,
                "mandate_anchor": "Article 42",
                "download_url": stub.raw_url,
                "contenthash": stub.hints.get("contenthash"),
                "require_recognized_legal_citation": True,
            },
        )
