"""Australian eSafety Online Safety Act codes, standards and regulatory guidance.

The eSafety Commissioner maintains two authoritative, compact publication lists:

* the Register of Online Safety Codes and Standards (including registered codes,
  standards, statutory directions and industry-representative notices); and
* the regulator's current guidance for the schemes administered under the
  Online Safety Act 2021 (Cth).

Both pages are live manifests.  Only PDFs linked from the page's main content are
harvested, and the page's own link label is retained as the canonical title.  A
Chrome-TLS client handles the site's bot filter cheaply; the configured Scrapling
service is the fallback.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    Stub,
    TypedRelation,
)
from ..extraction import extract_bytes

BASE = "https://www.esafety.gov.au"
REGISTER_URL = BASE + "/industry/codes/register-online-industry-codes-standards"
GUIDANCE_URL = BASE + "/industry/regulatory-guidance"
ONLINE_SAFETY_ACT_2021 = "au/cth/act/2021/76"
PAGE_URLS = (REGISTER_URL, GUIDANCE_URL)

_PDF_SUFFIX = re.compile(r"\s*\(\s*PDF\s*,[^)]*\)\s*$", re.I)
_UPDATED = re.compile(r"\s*\(\s*updated?\s*[-–—:]?\s*[^)]*\)\s*$", re.I)
_FULL_DATE = re.compile(
    r"\b(?P<day>\d{1,2})\s+(?P<month>January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+(?P<year>20\d{2})\b",
    re.I,
)
_URL_MONTH = re.compile(r"/(?P<year>20\d{2})-(?P<month>0[1-9]|1[0-2])/", re.I)
_ACT_PROVISIONS = re.compile(
    r"\b(?P<kind>sections?|ss?\.?)\s+"
    r"(?P<list>\d+[A-Za-z]?(?:\(\d+[A-Za-z]?\))*"
    r"(?:\s*(?:,|and|to|through|&|[-–—])\s*"
    r"\d+[A-Za-z]?(?:\(\d+[A-Za-z]?\))*)*)"
    r"\s+(?:of|under)\s+(?:this|the)\s+Act\b",
    re.I,
)
_ONE_PROVISION = re.compile(r"\d+[A-Za-z]?(?:\(\d+[A-Za-z]?\))*")
_PAGE_UPDATED = re.compile(r"Last updated:\s*(\d{2}/\d{2}/\d{4})", re.I)


def _clean(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _canonical_title(value: str) -> str:
    return _PDF_SUFFIX.sub("", _clean(value)).strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _date_from(*values: str) -> date | None:
    for value in values:
        match = _FULL_DATE.search(value or "")
        if match:
            try:
                return datetime.strptime(match.group(0), "%d %B %Y").date()
            except ValueError:
                pass
    return None


def _contextual_title(title: str, subsection: str, url: str) -> str:
    """Disambiguate the register's deliberately repeated notice/direction labels."""
    lower = title.casefold()
    if lower.startswith(("esafety notice:", "esafety variation notice:")):
        dated = _FULL_DATE.search(subsection)
        if dated and dated.group(0).casefold() not in lower:
            return f"{title} — {dated.group(0)}"
    if lower.startswith("direction to comply:"):
        # The provider is legally withheld, so the official URL's month is the only
        # public discriminator between otherwise identically titled directions.
        month = _URL_MONTH.search(url)
        if month:
            label = date(
                int(month.group("year")), int(month.group("month")), 1
            ).strftime("%B %Y")
            if label.casefold() not in lower:
                return f"{title} — {label}"
    return title


def _semantic_key(page_url: str, h2: str, h3: str, title: str) -> str:
    """Stable across a guidance PDF refresh, but distinct for dated statutory notices."""
    if page_url == GUIDANCE_URL:
        # The h2 is the enduring scheme name; labels add "(updated Month YYYY)".
        key = f"guidance|{h2 or _UPDATED.sub('', title)}"
    else:
        base_title = _UPDATED.sub("", title)
        # Registration / notice dates distinguish successive statutory instruments.
        key = f"register|{h2}|{h3}|{base_title}"
    digest = hashlib.sha1(key.casefold().encode("utf-8")).hexdigest()[:12]
    readable = _slug(h2 or title)[:70] or "document"
    collection = "guidance" if page_url == GUIDANCE_URL else "register"
    return f"au/esafety/{collection}/{readable}-{digest}"


def parse_esafety_page(raw: bytes | str, *, page_url: str) -> list[dict]:
    """Parse official PDFs from one eSafety manifest with heading context and titles."""
    soup = BeautifulSoup(raw, "html.parser")
    main = soup.select_one("main") or soup.select_one('[role="main"]')
    if main is None:
        return []

    page_text = _clean(main.get_text(" ", strip=True))
    page_updated = None
    updated = _PAGE_UPDATED.search(page_text)
    if updated:
        try:
            page_updated = datetime.strptime(updated.group(1), "%d/%m/%Y").date()
        except ValueError:
            pass

    out: list[dict] = []
    headings = {2: "", 3: "", 4: "", 5: ""}
    seen_urls: set[str] = set()
    for node in main.find_all(["h2", "h3", "h4", "h5", "strong", "a"]):
        if node.name in {"h2", "h3", "h4", "h5"}:
            level = int(node.name[1])
            headings[level] = _clean(node.get_text(" ", strip=True))
            for deeper in range(level + 1, 6):
                headings[deeper] = ""
            continue
        if node.name == "strong":
            # Drupal renders the individual notice dates as bold paragraphs rather
            # than semantic headings.  Treat only date-bearing bold labels as the
            # fourth-level heading; ordinary bold body text must not affect metadata.
            label = _clean(node.get_text(" ", strip=True))
            if _FULL_DATE.search(label):
                headings[4] = label
                headings[5] = ""
            continue
        href = _clean(str(node.get("href") or ""))
        if ".pdf" not in href.casefold():
            continue
        url = urljoin(BASE, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = _canonical_title(node.get_text(" ", strip=True))
        if not title:
            continue
        h2 = headings[2]
        subsection = " — ".join(
            value for level, value in headings.items() if level > 2 and value
        )
        title = _contextual_title(title, subsection, url)
        published = _date_from(title, subsection, h2)
        out.append({
            "stable_id": _semantic_key(page_url, h2, subsection, title),
            "title": title,
            "url": url,
            "landing_url": page_url,
            "section": h2,
            "subsection": subsection,
            "published": published,
            "page_updated": page_updated,
            "collection": "regulatory-guidance" if page_url == GUIDANCE_URL else "codes-standards-register",
        })
    return out


def online_safety_act_relations(text: str) -> list[TypedRelation]:
    """Base Act edge plus provisions explicitly qualified as being of ``the Act``.

    Code clause references such as "section 2.1 of the Head Terms" are deliberately
    not captured.  The source is a single-regime collection, but internal sections of
    codes and standards must not be misrepresented as provisions of the Act.
    """
    relations = [TypedRelation(
        relationship_type=RelationshipType.INTERPRETS,
        raw_citation_string="Online Safety Act 2021 (Cth)",
        dst_id=ONLINE_SAFETY_ACT_2021,
        extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.PENDING,
    )]
    seen: set[str] = set()
    for match in _ACT_PROVISIONS.finditer(text):
        for provision in _ONE_PROVISION.findall(match.group("list")):
            anchor = f"s. {provision}"
            if anchor.casefold() in seen:
                continue
            seen.add(anchor.casefold())
            relations.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=f"section {provision} of the Online Safety Act 2021 (Cth)",
                dst_id=ONLINE_SAFETY_ACT_2021,
                dst_anchor=anchor,
                extracted_via=ExtractedVia.REGEX,
                resolution_status=ResolutionStatus.PENDING,
            ))
    return relations


class ESafetyHTTP:
    """Chrome-fingerprint transport for eSafety, with ordinary HTTP as fallback."""

    def __init__(self, source: str, *, min_interval: float = 1.0, session=None) -> None:
        self.source = source
        self.min_interval = min_interval
        self._last = 0.0
        self._session = session
        self._fallback = None

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
        if response.status_code >= 400:
            raise FetchError(
                f"{self.source}: HTTP {response.status_code} for {url}",
                transient=response.status_code >= 500,
            )
        return response


class ESafetyOnlineSafetyAdapter(BaseAdapter):
    source = "au-esafety-osa"
    min_interval = 1.0
    # Listing fallback may use the shared Scrapling browser; serialize accordingly.
    requires_js = True

    def __init__(self, *, client=None, fetcher=None) -> None:
        self._client = client or ESafetyHTTP(
            self.source, min_interval=self.min_interval
        )
        self._fetcher = fetcher

    def _listing(self, url: str) -> str:
        try:
            response = self._client.get(url)
            content = response.content
            html = (
                content.decode("utf-8", "replace")
                if isinstance(content, bytes) else str(content)
            )
            if "<main" in html.casefold() and len(html) > 10_000:
                return html
        except Exception:  # noqa: BLE001 — bot wall: escalate to configured Scrapling
            pass
        if self._fetcher is None:
            from ..scraping.fetcher import get_fetcher

            self._fetcher = get_fetcher(
                "stealth", source=self.source, min_interval=self.min_interval,
                requires_js=True,
            )
        return self._fetcher.fetch(url).html

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        limit = len(PAGE_URLS) if max_pages is None else min(max_pages, len(PAGE_URLS))
        for page_url in PAGE_URLS[:limit]:
            for row in parse_esafety_page(self._listing(page_url), page_url=page_url):
                watermark = (
                    row["page_updated"].isoformat()
                    if row["page_updated"] else None
                )
                yield Stub(
                    stable_id=row["stable_id"],
                    landing_url=row["landing_url"],
                    raw_url=row["url"],
                    hint_date=row["published"],
                    title=row["title"],
                    court="eSafety Commissioner",
                    hints={
                        **row,
                        "watermark": watermark,
                        # A replacement URL/query token is the manifest's cheap
                        # signal that the current publication has changed.
                        "contenthash": row["url"],
                    },
                )

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url)
        raw = response.content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = (extracted.text or "").strip()
        if len(text) < 100:
            return None
        segments = [
            Segment(
                label=f"p. {page}", char_start=start, char_end=end, kind="page"
            )
            for page, start, end in (extracted.page_spans or [])
        ]
        section = stub.hints.get("section") or ""
        collection = stub.hints.get("collection") or ""
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=stub.title,
            court="eSafety Commissioner",
            decision_date=stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text,
            segments=segments,
            relations=online_safety_act_relations(text),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[
                "au", "online-safety", "esafety", "regulatory",
                _slug(section) or _slug(collection),
            ],
            extra={
                "jurisdiction": "au-cth",
                "issuer": "eSafety Commissioner",
                "regime": ONLINE_SAFETY_ACT_2021,
                "collection": collection,
                "category": section or None,
                "subcategory": stub.hints.get("subsection") or None,
                "download_url": stub.raw_url,
                "contenthash": stub.hints.get("contenthash"),
                "manifest_updated": (
                    stub.hints["page_updated"].isoformat()
                    if stub.hints.get("page_updated") else None
                ),
                # This is a statutorily bounded, single-regime collection.  The
                # structured Act edge satisfies the regulator relevance gate.
                "require_recognized_legal_citation": True,
            },
        )
