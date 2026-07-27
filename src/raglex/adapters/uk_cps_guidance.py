"""Crown Prosecution Service prosecution guidance library."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub
from ..extraction import extract_bytes

BASE = "https://www.cps.gov.uk"
LIBRARY_URL = BASE + "/prosecution-guidance-library"
_DATE = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4})\b",
    re.I,
)


def _clean(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _parse_date(value: str) -> date | None:
    match = _DATE.search(value or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%d %B %Y").date()
    except ValueError:
        return None


def parse_cps_library(raw: bytes | str) -> list[dict]:
    """Read the A–Z library, excluding navigation/search links."""
    soup = BeautifulSoup(raw, "html.parser")
    library = soup.select_one("main .az-library")
    if library is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    letter = ""
    for node in library.find_all(["h2", "a"]):
        if node.name == "h2":
            letter = _clean(node.get_text(" ", strip=True))
            continue
        href = _clean(str(node.get("href") or ""))
        title = _clean(node.get_text(" ", strip=True))
        if not href or not title:
            continue
        url = urljoin(BASE, href)
        parsed = urlsplit(url)
        is_guidance = (
            parsed.netloc.casefold().endswith("cps.gov.uk")
            and parsed.path.startswith("/prosecution-guidance/")
        )
        is_pdf = ".pdf" in parsed.path.casefold()
        if not (is_guidance or is_pdf) or url in seen:
            continue
        seen.add(url)
        if is_guidance:
            stable_id = f"uk/cps/guidance/{_slug(parsed.path.rsplit('/', 1)[-1])}"
        else:
            digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
            stable_id = f"uk/cps/guidance/pdf/{_slug(title)[:60]}-{digest}"
        out.append({
            "stable_id": stable_id,
            "title": title,
            "url": url,
            "letter": letter[:1].upper() if letter else None,
            "is_pdf": is_pdf,
        })
    return out


def parse_cps_guidance(raw: bytes | str) -> dict:
    """Extract the canonical title/date/category and structured body from one page."""
    soup = BeautifulSoup(raw, "html.parser")
    content = soup.select_one("main .cps-content")
    if content is None:
        return {"title": None, "date": None, "tags": [], "text": "", "segments": []}
    title_node = content.select_one("h1")
    date_node = content.select_one(".cps-content__date")
    tags_node = content.select_one(".cps-content__tags")
    body = content.select_one(".cps-content__body")
    if body is None:
        body = content

    parts: list[str] = []
    headings: list[tuple[str, int, int]] = []
    cursor = 0
    for node in body.find_all(["h2", "h3", "h4", "p", "li"]):
        if node.name == "p" and node.find_parent("li") is not None:
            continue
        # A nested list item's text includes its child list items; keep only the
        # leaf/own text to avoid duplicating whole subtrees.
        if node.name == "li":
            own = " ".join(
                str(child) for child in node.contents
                if getattr(child, "name", None) not in {"ul", "ol"}
            )
            value = _clean(BeautifulSoup(own, "html.parser").get_text(" ", strip=True))
        else:
            value = _clean(node.get_text(" ", strip=True))
        if not value:
            continue
        if parts:
            cursor += 2
        start = cursor
        parts.append(value)
        cursor += len(value)
        if node.name in {"h2", "h3", "h4"}:
            headings.append((value, start, int(node.name[1])))
    text = "\n\n".join(parts)
    segments: list[Segment] = []
    for index, (label, start, level) in enumerate(headings):
        end = headings[index + 1][1] - 2 if index + 1 < len(headings) else len(text)
        segments.append(Segment(
            label=label, char_start=start, char_end=max(start, end),
            kind="section", level=max(0, level - 2),
        ))
    tags = []
    if tags_node is not None:
        tags = [_clean(x.get_text(" ", strip=True)) for x in tags_node.select("a")]
        if not tags:
            tags = [_clean(tags_node.get_text(" ", strip=True))]
    return {
        "title": _clean(title_node.get_text(" ", strip=True)) if title_node else None,
        "date": _parse_date(_clean(date_node.get_text(" ", strip=True))) if date_node else None,
        "tags": [tag for tag in tags if tag],
        "text": text,
        "segments": segments,
    }


class CPSProsecutionGuidanceAdapter(BaseAdapter):
    source = "uk-cps-guidance"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        rows = parse_cps_library(self._client.get(LIBRARY_URL).content)
        # The library itself is one manifest page; max_pages bounds manifests, not
        # guidance records. A zero cap is the only value that suppresses it.
        if max_pages is not None and max_pages <= 0:
            return
        for row in rows:
            yield Stub(
                stable_id=row["stable_id"],
                landing_url=row["url"],
                raw_url=row["url"],
                title=row["title"],
                court="Crown Prosecution Service",
                hints=row,
            )

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url)
        raw = response.content
        is_pdf = bool(stub.hints.get("is_pdf")) or "pdf" in (
            response.headers.get("content-type") or ""
        ).casefold()
        if is_pdf:
            extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
            text = (extracted.text or "").strip()
            segments = [
                Segment(label=f"p. {page}", char_start=start, char_end=end, kind="page")
                for page, start, end in (extracted.page_spans or [])
            ]
            title, published, tags = stub.title, None, []
            ext = "pdf"
            needs_ocr = extracted.needs_ocr
        else:
            parsed = parse_cps_guidance(raw)
            text = parsed["text"]
            segments = parsed["segments"]
            title = parsed["title"] or stub.title
            published = parsed["date"]
            tags = parsed["tags"]
            ext = "html"
            needs_ocr = False
        if len(text or "") < 100:
            return None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=title,
            court="Crown Prosecution Service",
            decision_date=published,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=ext,
            text=text,
            segments=segments,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["uk", "criminal-law", "prosecution-guidance", *[
                _slug(tag) for tag in tags if tag
            ]],
            extra={
                "jurisdiction": "uk",
                "issuer": "Crown Prosecution Service",
                "library_letter": stub.hints.get("letter"),
                "categories": tags,
                "download_url": stub.raw_url if is_pdf else None,
                "needs_ocr": needs_ocr,
                # The library is legally focused but broad; retain/dedup every
                # item and suppress any genuine non-legal outlier from search.
                "require_recognized_legal_citation": True,
            },
        )
