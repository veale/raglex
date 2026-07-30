"""Italian AGCM weekly decision bulletins.

The Autorità Garante della Concorrenza e del Mercato publishes every operative
measure in a newest-first, sequential weekly bulletin.  The register is server
rendered and paged by a small POST form; each detail page links the official PDF.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.agcm.it"
LISTING = f"{BASE}/pubblicazioni/bollettino-settimanale/index"
_DETAIL_RE = re.compile(
    r"/pubblicazioni/bollettino-settimanale/(?P<year>\d{4})/(?P<issue>[^/]+)/",
    re.I,
)


def _italian_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def bulletin_stubs(html: bytes) -> list[Stub]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Stub] = []
    for row in soup.select("table tbody tr"):
        cells = row.find_all("td")
        link = row.select_one('a[href*="/pubblicazioni/bollettino-settimanale/"]')
        if len(cells) < 2 or not link:
            continue
        href = str(link.get("href") or "")
        match = _DETAIL_RE.search(href)
        published = _italian_date(cells[0].get_text(" ", strip=True))
        if not match or not published:
            continue
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        out.append(Stub(
            stable_id=f"it/agcm/bollettino/{match.group('year')}/{slug.lower()}",
            landing_url=urljoin(BASE, href),
            raw_url=urljoin(BASE, href),
            title=" ".join(link.get_text(" ", strip=True).split()),
            court="AGCM",
            hint_date=published,
            hints={
                "watermark": published.isoformat(),
                "contenthash": published.isoformat(),
                "issue": match.group("issue"),
                "year": match.group("year"),
            },
        ))
    return out


class AGCMBulletinAdapter(BaseAdapter):
    source = "it-agcm"
    min_interval = 1.0
    page_size = 50

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        page = 1
        while True:
            response = self._client.request(
                "POST", LISTING, data={"page": str(page), "limit": str(self.page_size)}
            )
            rows = bulletin_stubs(response.content)
            if not rows:
                return
            for stub in rows:
                watermark = str(stub.hints.get("watermark") or "")
                if since and watermark <= since[:10]:
                    return
                yield stub
            if len(rows) < self.page_size:
                return
            if max_pages is not None and page >= max_pages:
                return
            page += 1

    def fetch(self, stub: Stub) -> Record | None:
        try:
            detail = self._client.get(stub.raw_url)
        except FetchError:
            return None
        soup = BeautifulSoup(detail.content, "html.parser")
        link = soup.select_one('a[href*="/dotcmsdoc/bollettini/"][href$=".pdf"]')
        if not link:
            return None
        pdf_url = urljoin(BASE, str(link.get("href") or ""))
        try:
            response = self._client.get(pdf_url)
        except FetchError:
            return None
        raw = response.content
        if not raw.startswith(b"%PDF"):
            return None
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        if len(text) < 80:
            return None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=stub.title,
            court="Autorità Garante della Concorrenza e del Mercato",
            decision_date=stub.hint_date,
            language="it",
            source_language="it",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["consumer-law", "competition-law", "agcm", "italy"],
            extra={
                "jurisdiction": "it",
                "bulletin_issue": stub.hints.get("issue"),
                "bulletin_year": stub.hints.get("year"),
                "download_url": pdf_url,
                "contenthash": stub.hints.get("contenthash"),
                # A bulletin concatenates unrelated decisions under several legal
                # regimes. Never let an orphan "articolo 20" inherit the last law
                # named in the preceding decision; only explicit Italian grammar
                # matches are admitted.
                "disable_carry_forward": True,
                "require_recognized_legal_citation": False,
            },
        )
