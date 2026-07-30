"""Dutch ACM guidance (``Leidraden``) for businesses and consumer enforcement."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.acm.nl"
LISTING = f"{BASE}/nl/publicaties/voorlichting-aan-bedrijven/acm-leidraad"


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

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90
        )

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
