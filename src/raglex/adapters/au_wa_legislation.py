"""Western Australian current consolidated legislation.

The Parliamentary Counsel's Office publishes alphabetical in-force indexes for
Acts and subsidiary legislation.  Each row identifies both the law and the
current ``mrdoc`` rendition.  The latter changes when a consolidation is
republished, making the index a cheap live manifest: routine runs enumerate the
manifest and the pipeline fetches only new or changed renditions.
"""

from __future__ import annotations

import re
import string
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub

BASE = "https://www.legislation.wa.gov.au/legislation/statutes.nsf/"
_LAW_ID = re.compile(r"law_a(\d+)\.html", re.I)
_MRDOC = re.compile(r"mrdoc_(\d+)\.(?:htm|html|docx|pdf)", re.I)
_NUMBER = re.compile(r"\b0*(\d+)\s+of\s+(\d{4})\b", re.I)
_YEAR = re.compile(r"\b(1[789]\d{2}|20\d{2})\b")
_CONSOLIDATED = re.compile(
    r"\b(?:as at|consolidated (?:to|as at)|version (?:as at|from))\s+"
    r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    re.I,
)
_PROVISION = re.compile(
    r"^(?:(?:Part|Division|Subdivision|Schedule)\s+\w+|"
    r"(?:s\.\s*)?\d+[A-Z]?(?:[A-Z]|\.\d+)*\b)",
    re.I,
)


def parse_wa_index(raw: bytes | str, *, kind: str) -> list[dict]:
    """Parse one official in-force index page."""
    soup = BeautifulSoup(raw, "html.parser")
    out: list[dict] = []
    for row in soup.select("table.if tbody tr"):
        title_link = row.select_one("a.citation[href*='law_a']")
        if title_link is None:
            continue
        law_match = _LAW_ID.search(title_link.get("href") or "")
        download = next(
            (
                a for a in row.select("a[href*='mrdoc_']")
                if str(a.get("href") or "").lower().endswith((".htm", ".html"))
            ),
            None,
        )
        if not law_match or download is None:
            continue
        mrdoc = _MRDOC.search(download.get("href") or "")
        cells = row.find_all("td")
        number_text = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
        number_match = _NUMBER.search(number_text)
        title = " ".join(title_link.get_text(" ", strip=True).split())
        title_year = _YEAR.search(title)
        year = int(number_match.group(2)) if number_match else (
            int(title_year.group(1)) if title_year else None
        )
        number = int(number_match.group(1)) if number_match else None
        law_id = law_match.group(1)
        stable_id = (
            f"au/wa/{kind}/{year}/{number}"
            if year is not None and number is not None
            else f"au/wa/{kind}/law/{law_id}"
        )
        out.append({
            "stable_id": stable_id,
            "law_id": law_id,
            "mrdoc_id": mrdoc.group(1) if mrdoc else None,
            "title": title,
            "year": year,
            "number": number,
            "number_text": number_text,
            "landing_url": urljoin(BASE, (title_link.get("href") or "").replace("&amp;", "&")),
            "raw_url": urljoin(BASE, download.get("href") or ""),
            "kind": kind,
        })
    return out


def parse_wa_document(raw: bytes | str) -> tuple[str, list[Segment], date | None]:
    """Flatten the official HTML rendition while retaining citable provision seams."""
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup(["script", "style"]):
        node.decompose()
    blocks: list[str] = []
    labels: list[str | None] = []
    for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        text = " ".join(node.get_text(" ", strip=True).replace("\xa0", " ").split())
        if not text or (blocks and text == blocks[-1]):
            continue
        blocks.append(text)
        labels.append(text[:100] if _PROVISION.match(text) else None)
    text_parts: list[str] = []
    segments: list[Segment] = []
    pos = 0
    for block, label in zip(blocks, labels):
        if text_parts:
            text_parts.append("\n\n")
            pos += 2
        start = pos
        text_parts.append(block)
        pos += len(block)
        if label:
            segments.append(
                Segment(label=label, char_start=start, char_end=pos, kind="section")
            )
    text = "".join(text_parts)
    consolidated = None
    matches = _CONSOLIDATED.findall(text)
    if matches:
        candidates: list[date] = []
        for value in matches:
            for fmt in ("%d %B %Y", "%d %b %Y"):
                try:
                    candidates.append(datetime.strptime(value, fmt).date())
                    break
                except ValueError:
                    continue
        if candidates:
            consolidated = max(candidates)
    return text, segments, consolidated


class WesternAustraliaLegislationAdapter(BaseAdapter):
    source = "au-wa"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120
        )

    def discover(
        self, since: str | None, *, max_pages: int | None = None
    ) -> Iterator[Stub]:
        # The current mrdoc id is the per-document cursor. We must scan the small
        # manifest even when a global watermark exists, because rows are alphabetical.
        pages = 0
        for prefix, kind in (("actsif", "act"), ("subsif", "regulation")):
            for letter in string.ascii_lowercase:
                if max_pages is not None and pages >= max_pages:
                    return
                url = f"{BASE}{prefix}_{letter}.html"
                response = self._client.get(url)
                pages += 1
                for row in parse_wa_index(response.content, kind=kind):
                    yield Stub(
                        stable_id=row["stable_id"],
                        landing_url=row["landing_url"],
                        raw_url=row["raw_url"],
                        title=row["title"],
                        hint_date=date(row["year"], 1, 1) if row["year"] else None,
                        hints={
                            **row,
                            "watermark": row["mrdoc_id"],
                            "contenthash": row["mrdoc_id"],
                        },
                    )

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url)
        text, segments, consolidated = parse_wa_document(response.content)
        if len(text) < 100:
            return None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=stub.title,
            # The consolidation-as-at date is currency metadata, not the date the Act
            # was made. Using it here put future commencements in Explore as if they
            # were future legislation (for example a 2002 Act appeared as 2027).
            decision_date=stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=response.content,
            raw_ext="html",
            text=text,
            segments=segments,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["legislation", "western-australia", "current-consolidation"],
            extra={
                "jurisdiction": "au-wa",
                "instrument_type": stub.hints.get("kind"),
                "year": stub.hints.get("year"),
                "number": stub.hints.get("number"),
                "number_text": stub.hints.get("number_text"),
                "law_id": stub.hints.get("law_id"),
                "mrdoc_id": stub.hints.get("mrdoc_id"),
                "effective_date": consolidated.isoformat() if consolidated else None,
                "current_consolidation": True,
                "is_authoritative": True,
                "contenthash": stub.hints.get("contenthash"),
            },
        )
