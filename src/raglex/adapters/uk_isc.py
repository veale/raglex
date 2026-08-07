"""The Intelligence and Security Committee of Parliament — one page, thirty years of PDFs.

Everything the ISC has published sits on ``/reports/``: 106 publication blocks and 215
PDFs, from the 1995 Annual Report to the current one, grouped into Current Parliament,
Previous Parliaments (one accordion per Parliament back to 1992–97), Government Responses
and Transcripts.

Two things about getting it.

**The accordions are in the HTML; a browser is what hides them.** Fetched with a real
browser the page comes back at 14,780 bytes with four PDFs on it, because the collapsed
sections are dropped from the rendered body — read that way, the ISC's entire history
before 2025 simply does not exist. Fetched as markup it is 156,327 bytes with all 215.
The site does refuse a bare client, but only on headers: a request that sends a browser
``User-Agent`` **and** ``Accept``/``Accept-Language`` gets a 200. So this is a plain HTTP
adapter on purpose, and the browser tier would be a downgrade.

**The publications post type is not addressable.** ``wp-sitemap-posts-publications-1.xml``
lists 107 entries and every one of them 302s to ``/publications``, which is a 404. The
reports page is not a convenient index over some canonical per-document pages; it is the
only place the documents are published.

So a document here is a **PDF**, named by the link text that introduces it — which is
what distinguishes the report from its press notice, both of which are real publications
and are stored separately. The date comes from the block's own ``Published: March 1996``,
which is a month, so it is recorded as the first of that month.

**The old ones are scans.** ``1995_ISC_AR.pdf`` extracts exactly zero characters through
the born-digital parser and OCRs to its opening page — "Intelligence and Security
Committee Annual Report 1995 … Cm 3198 LONDON HMSO £5.00". Every PDF that comes back
without a text layer goes through :func:`raglex.extraction.ocr.text_or_ocr`, so a scanned
Command Paper enters the corpus as text rather than as an empty row flagged for later.
It is slow — roughly ten seconds a page — and that is the right trade for 107 documents.
"""

from __future__ import annotations

import html as _html
import logging
import re
from datetime import date, datetime
from typing import Iterator

from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub

log = logging.getLogger("raglex.adapters.uk_isc")

HOST = "https://isc.independent.gov.uk"
REPORTS = f"{HOST}/reports/"

#: The site 403s a request without these. It is not an anti-bot wall — it is a header
#: check, and a request that looks like a reader's is answered in full.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
                   "Gecko/20100101 Firefox/128.0 (+RagLex legal-research corpus)"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

_SECTION = re.compile(r'<h2[^>]*id="([^"]*)"[^>]*>(.*?)</h2>', re.S)
_ACCORDION = re.compile(r'<div class="accordion__content"[^>]*aria-labelledby="([^"]*)"',
                        re.S)
#: Publication blocks are found by SPLITTING on their opening marker, never by matching
#: a closing one. The divs inside are deeply nested and identical, so every terminator
#: that worked for the blocks in the middle had nothing to stop on for the last one —
#: which silently cost the page its final publication (106 of 107 on the live page).
_POST_MARKER = re.compile(r'<div class="publication-block__post">')
#: ``<a href="…pdf"><strong>ISC Annual Report 2022-2023</strong></a>`` — the label is in
#: the ``<strong>``; the sibling "Download" button repeats the href with no name.
_FILE = re.compile(
    r'<div class="icon icon--([a-z-]+)-file">.*?<a[^>]+href="([^"]+\.pdf)"[^>]*>'
    r'\s*(?:<strong>(.*?)</strong>)?', re.S | re.I)
_PUBLISHED = re.compile(r'class="publication-block__date"[^>]*>\s*Published:\s*(.*?)</p>',
                        re.S | re.I)

#: What the block's two file slots mean. ``pdf`` is the publication; ``press`` is the
#: notice issued with it — a separate document, not a duplicate of the first.
FILE_KINDS = {"pdf": "report", "press": "press-notice"}


def _clean(fragment: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-") or "document"


def parse_published(value: str | None) -> date | None:
    """``December 2023`` or ``March 1996`` — a month, recorded as its first day."""
    text = _clean(value or "")
    for fmt in ("%d %B %Y", "%B %Y", "%d %b %Y", "%b %Y", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _headings(html: str) -> list[tuple[int, str, str]]:
    """Every ``<h2 id=…>`` and accordion label, by position and by KIND.

    The kind has to come from which element it was, not from what the text looks like.
    Classifying on shape — a year range is a Parliament, anything else is a section —
    reads the Transcripts accordions ("2015", "2014", "2013") as section headings, so
    the last *real* Parliament label stays in force underneath them and thirty
    transcripts from 2013-15 are filed under the 1992-97 Parliament."""
    marks = [(m.start(), "section", _clean(m.group(2))) for m in _SECTION.finditer(html)]
    marks += [(m.start(), "period", _clean(m.group(1))) for m in _ACCORDION.finditer(html)]
    return sorted(marks)


def _context(marks: list[tuple[int, str, str]], position: int) -> tuple[str | None, str | None]:
    """``(section, period)`` for a block: the ``h2`` it sits under, and the accordion
    within that ``h2`` — which is a Parliament under "Previous Parliaments" and a year
    under "Transcripts". A new section always clears the period; accordions do not
    carry across the heading that ends their group."""
    section = period = None
    for start, kind, label in marks:
        if start > position:
            break
        if kind == "section":
            section, period = label, None
        else:
            period = label
    return section, period


def parse_reports(html: str) -> list[dict]:
    """The reports page → one row per published PDF, in page order."""
    html = html or ""
    marks = _headings(html)
    starts = [m.start() for m in _POST_MARKER.finditer(html)]
    out: list[dict] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(html)
        body = html[start:end]
        published = _PUBLISHED.search(body)
        when = parse_published(published.group(1)) if published else None
        section, period = _context(marks, start)
        for slot, url, label in _FILE.findall(body):
            url = _html.unescape(url).strip()
            if not url:
                continue
            title = _clean(label or "")
            out.append({
                "url": url,
                "title": title or _clean(url.rstrip("/").rsplit("/", 1)[-1][:-4]),
                "kind": FILE_KINDS.get(slot.split("-")[0], slot),
                "date": when,
                "section": section,
                "period": period,
            })
    # The same PDF is occasionally linked from two blocks (a report and the response to
    # it); the first occurrence carries the fuller context.
    seen: set[str] = set()
    unique = []
    for row in out:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        unique.append(row)
    return unique


class ISCReportsAdapter(BaseAdapter):
    source = "uk-isc"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None,
                 include_press: bool = True, ocr: bool = True,
                 max_ocr_pages: int = 200) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=300)
        self.include_press = option_flag(include_press, True)
        self.ocr = option_flag(ocr, True)
        self.max_ocr_pages = max(0, option_int(max_ocr_pages, 200))

    def _get(self, url: str) -> bytes | None:
        try:
            resp = self._client.get(url, headers=HEADERS)
        except FetchError:
            return None
        content = resp.content or b""
        return content if getattr(resp, "status_code", 200) < 400 and content else None

    # ---- discovery -----------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if max_pages is not None and max_pages <= 0:
            return          # the archive is one page; only a zero cap suppresses it
        cutoff = _as_date(since)
        rows = [r for r in parse_reports((self._get(REPORTS) or b"").decode("utf-8", "ignore"))
                if self.include_press or r["kind"] != "press-notice"]
        for row in rows:
            if cutoff and row["date"] and row["date"] < cutoff:
                continue
            yield Stub(
                stable_id=f"uk/isc/{slugify(row['url'].rstrip('/').rsplit('/', 1)[-1][:-4])}",
                landing_url=REPORTS, raw_url=row["url"],
                title=row["title"], hint_date=row["date"],
                hints={**row, "feed_total": len(rows),
                       "watermark": row["date"].isoformat() if row["date"] else None,
                       "date": row["date"].isoformat() if row["date"] else None},
            )

    # ---- fetch ---------------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        blob = self._get(stub.raw_url or "")
        if not blob or blob[:5] != b"%PDF-":
            log.warning("%s: %s is not a PDF", self.source, stub.raw_url)
            return None
        from ..extraction.ocr import text_or_ocr

        text, needs_ocr, spans, engine = text_or_ocr(
            blob, max_pages=self.max_ocr_pages if self.ocr else 0)
        if len(text.strip()) < 200:
            # Real, published, and unreadable by this image: keep it as a node with its
            # metadata so the flag is a worklist item rather than a silent omission.
            needs_ocr = True
        segments = [Segment(label=f"p. {n}", char_start=s, char_end=e, kind="page")
                    for n, s, e in spans]
        section = stub.hints.get("section")
        period = stub.hints.get("period")
        tags = ["uk", "isc", "intelligence-oversight", "parliament"]
        if stub.hints.get("kind"):
            tags.append(slugify(stub.hints["kind"]))
        if section:
            tags.append(slugify(section))
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.GUIDANCE,
            title=stub.title or stub.hints.get("title"),
            court="Intelligence and Security Committee of Parliament",
            decision_date=stub.hint_date or _as_date(stub.hints.get("date")),
            language="en", source_language="en", landing_url=REPORTS,
            raw_bytes=blob, raw_ext="pdf",
            text=text or None, segments=segments, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=list(dict.fromkeys(tags)),
            extra={
                "jurisdiction": "uk",
                "issuer": "Intelligence and Security Committee of Parliament",
                "document_kind": stub.hints.get("kind"),
                "section": section,
                "period": period,
                "pdf_url": stub.raw_url,
                "format": engine,
                "needs_ocr": needs_ocr or None,
                "licence": "Crown copyright",
                # The Committee's own mandate is the Justice and Security Act 2013, but
                # its reports range across RIPA, the IPA and the Intelligence Services
                # Act, so no default instrument is declared.
            },
        )


def _as_date(since: str | None) -> date | None:
    if not since:
        return None
    try:
        return datetime.strptime(str(since)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
