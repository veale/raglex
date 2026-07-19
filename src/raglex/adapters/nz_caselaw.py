"""New Zealand Supreme Court judgments — the Courts of NZ RSS feed (§1.5).

The court publishes an RSS feed of every Supreme Court judgment (2004–present). This
adapter walks it, fetches each case page on ``courtsofnz.govt.nz``, finds the judgment
PDF, and parses it into text + numbered-paragraph segments, keying the case by the
**neutral citation printed in the PDF** ("[2026] NZSC 88" → ``nzsc/2026/88``) so every
"[2026] NZSC 88" reference elsewhere in the corpus resolves to it. Party names come from
the case page's HTML heading (the RSS title is terse — "Re Rafiq").

Discovery is incremental: the RSS ``pubDate`` is the watermark, so a keep-current run
only fetches judgments published since the last pass. A backfill (ignore-watermark) walks
the whole feed. Politeness starts at a 10s floor between requests; the rate-limited client
automatically backs further off on a 429/503 (§1.8), so a block widens the interval.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Iterator
from urllib.parse import urlsplit, urljoin

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats.nzsc_pdf import file_number_id, parse_nzsc_pdf

BASE_URL = "https://www.courtsofnz.govt.nz"
RSS_URL = f"{BASE_URL}/the-courts/supreme-court/judgments-supreme/RSS"

# A polite floor; the court is a small government service. The rate-limited client widens
# this automatically on a 429/503, so "increase if blocked" is handled for us.
_MIN_INTERVAL = 10.0

# A media-release PDF sits beside the judgment on some case pages; never the judgment.
_MEDIA_RE = re.compile(r"/MR[-_]", re.IGNORECASE)
# "2026-NZSC-88.pdf" → the neutral citation, the fallback identity when the PDF text
# somehow lacks it (older files aren't citation-named, so this is best-effort).
_FILENAME_CITE_RE = re.compile(r"(?P<year>(?:19|20)\d{2})[-_]NZSC[-_](?P<num>\d+)", re.IGNORECASE)
# the "- [2026] NZSC 91" / "- SC CRI 2/2004" citation suffix a case heading appends to the
# party line — stripped so the title is just the parties.
_CITE_SUFFIX_RE = re.compile(r"\s*[-–—]\s*(?:\[\d{4}\].*|SC\b.*\d{4}.*)$")


@dataclass(slots=True)
class _Item:
    title: str
    url: str
    when: datetime | None


class NZSupremeCourtAdapter(BaseAdapter):
    source = "nz-caselaw"
    court = "nzsc"
    min_interval = _MIN_INTERVAL
    requires_js = False
    requires_proxy = False

    def __init__(self, *, client: RateLimitedClient | None = None,
                 rss_url: str = RSS_URL, rss_path: str | None = None) -> None:
        self.rss_url = rss_url
        self.rss_path = rss_path  # optional local fallback (the vendored RSS-2.rss)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=60)

    # -- discover -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        items = self._rss_items()
        seen: set[str] = set()
        yielded = 0
        for it in items:
            if it.url in seen:            # the feed carries near-duplicate rows; one per URL
                continue
            seen.add(it.url)
            wm = it.when.isoformat() if it.when else None
            # incremental: skip anything at or before the watermark (a strictly-after feed,
            # matching the shared cursor convention). Backfill passes since=None → yield all.
            if since and wm and wm <= since:
                continue
            yield Stub(
                stable_id=_provisional_id(it.url),   # real id is the PDF's neutral citation
                title=it.title or None,
                court=self.court,
                landing_url=it.url,
                hint_date=it.when.date() if it.when else None,
                hints={"watermark": wm} if wm else {},
            )
            yielded += 1
            if max_pages is not None and yielded >= max_pages:
                return

    def _rss_items(self) -> list[_Item]:
        """Fetch + parse the RSS feed (live URL, or the local fallback if given/needed)."""
        raw: bytes | None = None
        try:
            raw = self._client.get(self.rss_url).content
        except FetchError:
            raw = None
        if not raw and self.rss_path:
            with open(self.rss_path, "rb") as fh:
                raw = fh.read()
        if not raw:
            return []
        return _parse_rss(raw)

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        # 1. the case page → party-name heading + the judgment PDF link
        try:
            page = self._client.get(stub.landing_url).text
        except FetchError as exc:
            if exc.transient:
                raise           # not an absence — retry, don't cool onto the miss list
            return None
        pdf_href = _find_pdf_href(page)
        if not pdf_href:
            return None         # no judgment PDF on this page — a genuine absence
        title = _party_title(page) or stub.title

        # 2. the PDF → text, paragraph/footnote segments, and the neutral citation
        pdf_url = urljoin(BASE_URL, pdf_href)
        try:
            raw = self._client.get(pdf_url).content
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        parsed = parse_nzsc_pdf(raw)
        if not (parsed.text or "").strip():
            return None

        # Identity ladder: the neutral citation when the case has one (NZSC 2005+); else
        # the court file number (the pre-2005 unreported-citation key — stable across the
        # feed's duplicate case-page URLs, which share it); else the URL slug.
        stable_id = (parsed.neutral_citation
                     or _filename_citation(pdf_url)
                     or file_number_id(parsed.file_number)
                     or _provisional_id(stub.landing_url))
        # The PDF's own "Judgment:" date is the true delivery date — better than the RSS
        # pubDate; fall back to the feed date.
        dec_date = parsed.judgment_date or stub.hint_date
        # Party names for the title: the case-page heading (clean, mixed-case) is best for
        # display; the PDF intituling ("ALAN IVO GREER v THE QUEEN") is the fullest for
        # matching and is kept in extra regardless.
        title = title or (parsed.parties.title() if parsed.parties else None)
        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.JUDGMENT,
            title=title or stable_id,
            court=self.court,
            decision_date=dec_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=parsed.text,
            segments=parsed.segments,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in {
                "jurisdiction": "nz",
                # identity / matching signal — richest for pre-2005 cases with no cite
                "neutral_citation": _pretty_citation(parsed.neutral_citation),
                "file_number": parsed.file_number,      # "SC CRI 2/2004"
                "parties": parsed.parties,              # full intituling party line
                "coram": parsed.coram or None,          # ["Elias CJ", "Blanchard J"]
                "counsel": parsed.counsel,
                "judgment_date": parsed.judgment_date.isoformat() if parsed.judgment_date else None,
                "pdf_url": pdf_url,
                "footnote_count": len(parsed.footnotes) or None,
            }.items() if v not in (None, "", [])},
        )


# -- RSS -------------------------------------------------------------------
def _parse_rss(raw: bytes) -> list[_Item]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items: list[_Item] = []
    for el in root.findall(".//item"):
        link = el.findtext("link")
        if not link:
            continue
        title = (el.findtext("title") or "").strip()
        items.append(_Item(title=title, url=link.strip(), when=_parse_pubdate(el.findtext("pubDate"))))
    return items


def _parse_pubdate(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s.strip())
    except (TypeError, ValueError):
        return None


# -- case page HTML --------------------------------------------------------
def _find_pdf_href(html: str) -> str | None:
    """The judgment PDF URL. Primary selector is the decision block; fall back to any
    /assets/cases/ PDF that isn't a media release (mirrors the vendored scraper)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    primary = soup.select(".case__decision a[href$='.pdf']")
    if primary:
        return primary[0].get("href")
    for a in soup.find_all("a", href=re.compile(r"/assets/cases/.*\.pdf$", re.IGNORECASE)):
        href = a.get("href") or ""
        if not _MEDIA_RE.search(href):
            return href
    return None


def _party_title(html: str) -> str | None:
    """Case/party name from the page — the richest heading available. RSS titles are
    terse ("Re Rafiq"); the case page usually carries the full party line."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    def _clean(s: str | None) -> str | None:
        s = re.sub(r"\s+", " ", s or "").strip()
        # drop a trailing site-name suffix the <title> carries ("… — Courts of New Zealand")
        s = re.split(r"\s+[—|]\s+", s)[0].strip()
        # drop the "- [2026] NZSC 91" / "- SC CRI 2/2004" citation suffix the heading appends,
        # leaving just the party line.
        s = _CITE_SUFFIX_RE.sub("", s).strip()
        return s if len(s) > 2 else None

    # The case-title heading (h1.case__title) is the party line. NB: this site's og:title is
    # a stale template (it can name an unrelated case), so it is deliberately NOT trusted;
    # the <title> is the fallback (correct, just site-name-suffixed).
    for sel in (".case__title", "h1"):
        el = soup.select_one(sel)
        if el and (t := _clean(el.get_text(" ", strip=True))):
            return t
    if soup.title and (t := _clean(soup.title.string)):
        return t
    return None


# -- identity --------------------------------------------------------------
def _provisional_id(case_url: str) -> str:
    """A stable, URL-derived id for the discovery stub (and last-resort identity when a
    case has no recoverable neutral citation) — ``nz-caselaw/<slug>``."""
    slug = urlsplit(case_url).path.rstrip("/").rsplit("/", 1)[-1] or "case"
    slug = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
    return f"nz-caselaw/{slug}"


def _filename_citation(pdf_url: str) -> str | None:
    m = _FILENAME_CITE_RE.search(urlsplit(pdf_url).path)
    return f"nzsc/{m.group('year')}/{int(m.group('num'))}" if m else None


def _pretty_citation(canonical: str | None) -> str | None:
    """``nzsc/2026/88`` → "[2026] NZSC 88" for display/metadata."""
    if not canonical:
        return None
    m = re.match(r"nzsc/(\d{4})/(\d+)", canonical)
    return f"[{m.group(1)}] NZSC {m.group(2)}" if m else None
