"""SPICe research briefings — the Scottish Parliament's research service.

Discovery is one server-rendered listing, and the only hard part is making it show you
anything. **The search defaults to a narrow recent window**: asked with no parameters it
reports 24 briefings, which looks like a complete answer and is a few weeks of them.

The date control is a fixed set of presets, and it does not take a range you invent.
Each option's value is ``{guid}|{From}|{To}`` — and the guid is bound server-side to its
own range, so the same guid carrying different dates returns zero results, as does
``dateSelect=custom`` with ``dtDateFrom``/``dtDateTo``. Passing ``dtDateFrom``/
``dtDateTo`` *alone* is worse than useless: the parameters are accepted, ignored, and the
unfiltered result set comes back looking filtered.

So the adapter **reads the option list off the form on every run** and picks the widest
preset, rather than hard-coding the URL a browser happened to produce. It has to: the
"all time" preset's label ends at *today*, so its value changes daily and a URL copied
from the address bar stops working tomorrow.

That widest preset returns 750 briefings back to 19 April 2017, and that is the whole of
it — not a cap. The Session 5 preset (May 2016 – May 2021) independently returns 381 with
the *same* 2017 floor, and 381 + the Session 6 preset's 345 accounts for the 750. Earlier
SPICe papers are not in this index at all; they are not silently truncated here.

**The briefing text comes from the PDF.** Each briefing's own page splits its body across
several paginated HTML views, and every one of them publishes the complete document at
``{page}/pdf`` — one request instead of walking a pager and reassembling it.
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

log = logging.getLogger("raglex.adapters.scot_spice")

HOST = "https://www.parliament.scot"
LISTING = f"{HOST}/chamber-and-committees/research-prepared-for-parliament/research-briefings"

#: The site answers a plain client, but only one that looks like a reader.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
                   "Gecko/20100101 Firefox/128.0 (+RagLex legal-research corpus)"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

PAGE_SIZE = 50          # the largest the listing offers

_DATE_OPTIONS = re.compile(
    r'(?s)<select[^>]*name="dateSelect"[^>]*>(.*?)</select>')
_OPTION = re.compile(r'<option[^>]*value="([^"]*)"', re.I)
_RESULT = re.compile(
    r'(?s)<div class="content-list__block">\s*<h2[^>]*>\s*'
    r'<a href="(/chamber-and-committees/research-prepared-for-parliament/'
    r'research-briefings/[^"]+)"[^>]*>(.*?)</a>\s*</h2>(.*?)</div>')
_SUBJECT = re.compile(r"<p>\s*Subject:\s*(.*?)</p>", re.S | re.I)
_NUMBER = re.compile(r"<p>\s*Briefing number:\s*(.*?)</p>", re.S | re.I)
_PUBLISHED = re.compile(r"<p>\s*Published:\s*(.*?)</p>", re.S | re.I)
_TOTAL = re.compile(r'id="reportResultCount"[^>]*>\s*Displaying\s+([\d,]+)', re.I)
_PAGES = re.compile(r'class="pagination__title"[^>]*>\s*Page:\s*\d+\s*of\s*(\d+)', re.I)
#: The briefing page's own metadata, which the listing does not carry.
_AUTHORS = re.compile(r'class="authors"[^>]*>\s*Author\(s\):\s*(.*?)</p>', re.S | re.I)
_SUMMARY = re.compile(r'class="summary"[^>]*>(.*?)</p>', re.S)
_TITLE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.S)


def _clean(fragment: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_date(value: str | None) -> date | None:
    """``05 August 2026`` — the only form either the listing or the page uses."""
    text = _clean(value or "")
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def widest_date_option(listing_html: str) -> str | None:
    """The ``dateSelect`` value spanning the longest period.

    Chosen by parsing each option's own ``From``/``To`` labels rather than by position,
    because the list is ordered newest-window-first and the archive-wide option sits in
    the middle of it."""
    block = _DATE_OPTIONS.search(listing_html or "")
    if not block:
        return None
    best, best_span = None, -1
    for value in _OPTION.findall(block.group(1)):
        value = _html.unescape(value)
        parts = value.split("|")
        if len(parts) != 3:
            continue        # "All" and "custom" carry no range and filter nothing
        start, end = _label_date(parts[1]), _label_date(parts[2])
        if not start or not end:
            continue
        span = (end - start).days
        if span > best_span:
            best, best_span = value, span
    return best


def _label_date(label: str) -> date | None:
    """``Wednesday, May 12, 1999`` — the option label's own date format."""
    try:
        return datetime.strptime(_clean(label), "%A, %B %d, %Y").date()
    except ValueError:
        return None


def parse_results(html: str) -> list[dict]:
    """One listing page → its briefings, in page order (newest first)."""
    out: list[dict] = []
    for path, title, rest in _RESULT.findall(html or ""):
        subject = _SUBJECT.search(rest)
        number = _NUMBER.search(rest)
        published = _PUBLISHED.search(rest)
        out.append({
            "url": HOST + _html.unescape(path),
            "slug": _html.unescape(path).rstrip("/").rsplit("/", 1)[-1],
            "title": _clean(title),
            "subject": _clean(subject.group(1)) if subject else None,
            "briefing_number": _clean(number.group(1)) if number else None,
            "date": parse_date(published.group(1)) if published else None,
        })
    return out


def result_total(html: str) -> int | None:
    found = _TOTAL.search(html or "")
    return int(found.group(1).replace(",", "")) if found else None


def page_total(html: str) -> int | None:
    found = _PAGES.search(html or "")
    return int(found.group(1)) if found else None


class SPICeBriefingsAdapter(BaseAdapter):
    source = "scot-spice"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None,
                 date_select: str | None = None, subject: str | None = None,
                 slugs: str | None = None, page_size: int = PAGE_SIZE,
                 ocr: bool = True) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)
        self.date_select = (date_select or "").strip() or None
        self.subject = (subject or "").strip() or None
        self.page_size = max(1, min(option_int(page_size, PAGE_SIZE), PAGE_SIZE))
        self.ocr = option_flag(ocr, True)
        self.slugs = tuple(s.strip() for s in str(slugs).split(",") if s.strip()) \
            if slugs else ()

    # ---- plumbing ------------------------------------------------------------------

    def _get(self, url: str, *, params: dict | None = None) -> bytes | None:
        try:
            resp = self._client.get(url, params=params, headers=HEADERS)
        except FetchError:
            return None
        content = resp.content or b""
        return content if getattr(resp, "status_code", 200) < 400 and content else None

    def _listing(self, params: dict | None = None) -> str:
        return (self._get(LISTING, params=params) or b"").decode("utf-8", "ignore")

    def _date_select(self) -> str | None:
        """The widest preset, read from the form now. Cached for the run — the option
        list changes daily (its label ends at today), not mid-harvest."""
        if self.date_select is None:
            self.date_select = widest_date_option(self._listing()) or ""
            if not self.date_select:
                log.warning("%s: no dateSelect options on the listing; the search will "
                            "answer with its default few-week window", self.source)
        return self.date_select or None

    # ---- discovery -----------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.slugs:
            for slug in self.slugs:
                yield from self._by_slug(slug)
            return
        cutoff = _as_date(since)
        params: dict = {"qry": "", "subjectSelect": self.subject or "", "reportid": "",
                        "pgsize": self.page_size}
        chosen = self._date_select()
        if chosen:
            params["dateSelect"] = chosen
        total = pages = None
        seen: set[str] = set()
        page = 1
        while True:
            html = self._listing({**params, "page": page} if page > 1 else params)
            rows = parse_results(html)
            if not rows:
                return
            if total is None:
                total, pages = result_total(html), page_total(html)
            for row in rows:
                if not row["slug"] or row["slug"] in seen:
                    continue
                seen.add(row["slug"])
                if cutoff and row["date"] and row["date"] < cutoff:
                    continue    # newest-first, but keep walking the page we are on
                yield Stub(
                    stable_id=f"scot/spice/{row['slug']}",
                    landing_url=row["url"], raw_url=f"{row['url']}/pdf",
                    title=row["title"], hint_date=row["date"],
                    hints={**row, "feed_total": total,
                           "resume_offset": (page - 1) * self.page_size,
                           "watermark": row["date"].isoformat() if row["date"] else None},
                )
            if cutoff and rows and all(
                    r["date"] and r["date"] < cutoff for r in rows):
                return          # a whole page older than the cursor: nothing newer below
            if pages is not None and page >= pages:
                return
            if max_pages is not None and page >= max_pages:
                return
            page += 1

    def _by_slug(self, slug: str) -> Iterator[Stub]:
        """A targeted fetch has only the id, and the URL needs the publication date in
        its path — so the id is looked up through the search rather than guessed."""
        html = self._listing({"qry": slug, "pgsize": self.page_size,
                              "dateSelect": self._date_select() or ""})
        for row in parse_results(html):
            if row["slug"].lower() == slug.lower():
                yield Stub(stable_id=f"scot/spice/{row['slug']}",
                           landing_url=row["url"], raw_url=f"{row['url']}/pdf",
                           title=row["title"], hint_date=row["date"], hints=row)
                return
        log.warning("%s: %s is not in the search index", self.source, slug)

    # ---- fetch ---------------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        landing = stub.landing_url or ""
        page_html = (self._get(landing) or b"").decode("utf-8", "ignore")
        blob = self._get(stub.raw_url or f"{landing}/pdf")
        text, segments, raw, raw_ext, fmt, needs_ocr = "", [], None, "html", "page", False
        if blob and blob[:5] == b"%PDF-":
            from ..extraction.ocr import text_or_ocr

            text, needs_ocr, spans, fmt = text_or_ocr(
                blob, max_pages=200 if self.ocr else 0)
            segments = [Segment(label=f"p. {n}", char_start=s, char_end=e, kind="page")
                        for n, s, e in spans]
            raw, raw_ext = blob, "pdf"
        if len(text.strip()) < 200 and page_html:
            # No PDF (or an unreadable one): the paginated HTML view is worse but real.
            fallback = _clean(re.sub(r"(?s)<(script|style|nav|footer).*?</\1>", " ",
                                     page_html))
            if len(fallback) > len(text):
                text, raw, raw_ext, fmt, segments = (
                    fallback, page_html.encode("utf-8"), "html", "page", [])
        if len(text.strip()) < 200:
            return None

        title = (_clean(_TITLE.search(page_html).group(1)) if _TITLE.search(page_html)
                 else None) or stub.title or stub.hints.get("title")
        authors = _AUTHORS.search(page_html)
        summary = _SUMMARY.search(page_html)
        subject = stub.hints.get("subject")
        number = stub.hints.get("briefing_number")
        tags = ["scotland", "scottish-parliament", "spice", "research-briefing"]
        if subject:
            tags.append(re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-"))
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.COMMENTARY,
            title=title, court="Scottish Parliament Information Centre (SPICe)",
            decision_date=stub.hint_date, language="en", source_language="en",
            landing_url=landing, raw_bytes=raw, raw_ext=raw_ext,
            text=text, segments=segments, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=list(dict.fromkeys(tags)),
            extra={
                "jurisdiction": "gb-sct",
                "issuer": "Scottish Parliament Information Centre (SPICe)",
                "briefing_number": number,
                "subject": subject,
                "summary": _clean(summary.group(1)) if summary else None,
                "authors": [a.strip() for a in
                            re.split(r",| and ", _clean(authors.group(1)))
                            if a.strip()] if authors else [],
                "pdf_url": stub.raw_url,
                "format": fmt,
                "needs_ocr": needs_ocr or None,
                "licence": "Scottish Parliamentary Corporate Body copyright",
                # SPICe covers the whole devolved policy field; retain everything and
                # let the grammars decide what is legal material.
                "require_recognized_legal_citation": True,
            },
        )


def _as_date(since: str | None) -> date | None:
    if not since:
        return None
    try:
        return datetime.strptime(str(since)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
