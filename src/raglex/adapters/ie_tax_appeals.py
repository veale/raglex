"""Irish Tax Appeals Commission determinations (official TAC PDFs)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.taxappeals.ie"
LISTING = BASE + "/en/determinations/"
_REF = re.compile(r"\b(?P<num>\d+)\s*TACD\s*(?P<year>(?:20)\d{2})\b", re.I)


def parse_reference(value: str) -> tuple[str, int, int] | None:
    match = _REF.search(value or "")
    if not match:
        return None
    year, number = int(match.group("year")), int(match.group("num"))
    return f"{number}TACD{year}", year, number


def parse_determinations_page(raw: bytes | str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for link in soup.select("a[href*='/en/determinations/']"):
        title = " ".join(link.get_text(" ", strip=True).split())
        parsed = parse_reference(title)
        if not parsed:
            continue
        ref, year, number = parsed
        if ref in seen:
            continue
        seen.add(ref)
        href = urljoin(BASE, link.get("href") or "")
        tax_type = re.sub(
            rf"^\s*{number}\s*TACD\s*{year}\s*[-–—:]?\s*", "", title,
            flags=re.I,
        ).strip()
        out.append({
            "reference": ref,
            "year": year,
            "number": number,
            "tax_type": tax_type,
            "title": title,
            "landing_url": href,
        })
    return out


def parse_determination_detail(raw: bytes | str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    pdf = soup.select_one("a[href$='.pdf']")
    text = " ".join(soup.get_text(" ", strip=True).split())
    published = None
    match = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})\b",
        text,
        re.I,
    )
    if match:
        cleaned = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", match.group(1), flags=re.I)
        try:
            published = datetime.strptime(cleaned, "%d %B %Y").date()
        except ValueError:
            pass
    return {
        "pdf_url": urljoin(BASE, pdf.get("href") or "") if pdf else None,
        "published": published,
    }


class TaxAppealsHTTP:
    """Chrome-TLS first; linked Scrapling fallback for blocked HTML listings."""

    def __init__(
        self, source: str, *, min_interval: float = 1.0, session=None, fetcher=None
    ) -> None:
        self.source = source
        self.min_interval = min_interval
        self._last = 0.0
        self._session = session
        self._fallback = None
        self._fetcher = fetcher

    def get(self, url: str, **kwargs):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        if self._session is None:
            try:
                from curl_cffi import requests as creq
            except ImportError:
                if self._fallback is None:
                    self._fallback = RateLimitedClient(
                        self.source, min_interval=self.min_interval, timeout=120
                    )
                return self._fallback.get(url, **kwargs)
            self._session = creq.Session(impersonate="chrome124", timeout=120)
        response = self._session.get(url, **kwargs)
        is_pdf = urlsplit(url).path.lower().endswith(".pdf")
        if response.status_code in {403, 429, 503} and not is_pdf:
            if self._fetcher is None:
                from ..scraping.fetcher import get_fetcher

                self._fetcher = get_fetcher(
                    "stealth", source=self.source, min_interval=self.min_interval,
                    requires_js=True,
                )
            page = self._fetcher.fetch(url, headers=kwargs.get("headers"))
            if page.status < 400 and page.html:
                @dataclass(slots=True)
                class _Response:
                    content: bytes
                    status_code: int = 200

                return _Response(page.html.encode())
        if response.status_code >= 400:
            raise FetchError(
                f"{self.source}: HTTP {response.status_code} for {url}",
                transient=response.status_code >= 500,
            )
        return response


class IrishTaxAppealsAdapter(BaseAdapter):
    source = "ie-tax-appeals"
    min_interval = 1.0

    def __init__(self, *, client=None) -> None:
        self._client = client or TaxAppealsHTTP(
            self.source, min_interval=self.min_interval
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        page = 1
        while True:
            url = LISTING if page == 1 else f"{LISTING}{page}"
            rows = parse_determinations_page(self._client.get(url).content)
            if not rows:
                return
            stop = False
            for row in rows:
                watermark = f"{row['year']:04d}-{row['number']:06d}"
                if since and watermark <= since:
                    stop = True
                    continue
                yield Stub(
                    stable_id=f"tacd/{row['year']}/{row['number']}",
                    landing_url=row["landing_url"],
                    title=row["title"],
                    court="TACD",
                    hints={
                        **row,
                        "watermark": watermark,
                        "contenthash": row["landing_url"],
                    },
                )
            page += 1
            if stop or (max_pages is not None and page > max_pages):
                return

    def fetch(self, stub: Stub) -> Record | None:
        detail = parse_determination_detail(
            self._client.get(stub.landing_url).content
        )
        pdf_url = detail["pdf_url"] or (
            f"{BASE}/_fileupload/Determinations/{stub.hints['year']}/"
            f"{stub.hints['reference']}.pdf"
        )
        raw = self._client.get(pdf_url).content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        if len(text) < 100:
            return None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=stub.title,
            court="TACD",
            decision_date=detail["published"],
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["ireland", "tax", "tribunal", "determination"],
            extra={
                "jurisdiction": "ie",
                "reference": stub.hints.get("reference"),
                "tax_type": stub.hints.get("tax_type"),
                "year": stub.hints.get("year"),
                "download_url": pdf_url,
                "aliases": [stub.hints.get("reference")],
                "contenthash": stub.hints.get("contenthash"),
            },
        )
