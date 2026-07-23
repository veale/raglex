"""High Court of Australia — judgments index (hcourt.gov.au).

The HCA judgments listing (``/cases-and-judgments/judgments/judgments-1998-current``)
is server-rendered and, filtered to one year with ``items_per_page=100``, holds every
judgment for that year on a single page — the Court delivers well under 100 a year, so
there is **no pagination** to walk. Each row carries the case name, the medium neutral
citation ("[2026] HCA 22"), the coram, the decision date, and a link to the judgment.

**Fetching is the hard part.** The site sits behind a WAF that admits only a *real
desktop Chrome*: plain HTTP is 403'd, and both the stealth tier's Camoufox (Firefox)
and a container's bundled/headless Chromium are blocked. So there are two ways in:

* ``path=`` — **import saved listing HTML** (a year page fetched in Chrome and saved).
  The immediately-working path given the tiny per-year volume: drop the files in and
  every judgment becomes a node.
* live — fetch each year's listing through a Chromium fetcher with ``real_chrome``
  (works once real Chrome is available to the scraper). ``years=`` selects which.

Either way a row becomes a **metadata-stub judgment** (like the CanLII adapter): keyed
by the neutral-citation slug :func:`au_case_slug` mints (``hca/2026/22``) — so it unifies
with the OALC bulk and resolves the "[2026] HCA 22" citations the corpus already holds —
with the coram, date and a verified "view on the High Court" link, but no body text yet
(the judgment pages are behind the same WAF; full text is a later enrichment once a
real-Chrome fetch is wired). Its identity and link make it useful the moment it lands.
"""

from __future__ import annotations

import html as _html
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.models import DocType, ExtractedVia, Record, Stub
from .au_caselaw import au_case_slug

BASE = "https://www.hcourt.gov.au"
LISTING = (BASE + "/cases-and-judgments/judgments/judgments-1998-current"
           "?keywords=&case_number=&items_per_page=100&f%5B0%5D=d%3A{year}")
_FIRST_YEAR = 1998


# ── pure parsing ─────────────────────────────────────────────────────────────
def _field(row: str, pattern: str) -> str | None:
    m = re.search(pattern, row, re.S)
    if not m:
        return None
    return _html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip()) or None


def parse_listing(html: str) -> list[dict]:
    """One HCA year-listing page (pure) → its judgments (newest-first as rendered)."""
    out: list[dict] = []
    for row in re.split(r'<div class="views-row">', html)[1:]:
        cite = _field(row, r'field--citation.*?</strong>(.*?)</div>')
        slug = au_case_slug(cite or "")
        if not slug:
            continue
        href = re.search(r'href="([^"]+)"', row)
        d = _hca_date(_field(row, r'field--hca-date-issued.*?</strong>(.*?)</div>'))
        out.append({
            "slug": slug,
            "citation": cite,
            "title": _field(row, r'field--title[^>]*>(.*?)<'),
            "coram": _field(row, r'field-hca-justices[^>]*>.*?</strong>(.*?)</div>'),
            "date": d.isoformat() if d else None,
            "url": _html.unescape(href.group(1)) if href else None,
        })
    return out


class HCACaselawAdapter(BaseAdapter):
    source = "au-hca"
    min_interval = 4.0
    requires_js = True
    requires_proxy = False

    def __init__(self, *, path: str | None = None, years: str | None = None,
                 fetcher=None) -> None:
        self.path = Path(path) if path else None
        self.years = _year_range(years)
        self._fetcher = fetcher

    def _get(self):
        if self._fetcher is None:
            from ..scraping.fetcher import get_fetcher
            self._fetcher = get_fetcher("stealth", source=self.source,
                                        min_interval=self.min_interval, requires_js=True)
        return self._fetcher

    # -- discovery -------------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is not None:
            yield from self._discover_saved()
            return
        for year in self.years:
            try:
                html = self._get().fetch(LISTING.format(year=year)).html or ""
            except FetchError:
                continue
            for j in parse_listing(html):
                if since and j["date"] and j["date"] <= since:
                    continue
                yield self._stub(j)

    def _discover_saved(self) -> Iterator[Stub]:
        files = ([self.path] if self.path.is_file()
                 else sorted(self.path.rglob("*.html")) if self.path.is_dir() else [])
        seen: set[str] = set()
        for fp in files:
            try:
                html = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for j in parse_listing(html):
                if j["slug"] in seen:
                    continue
                seen.add(j["slug"])
                yield self._stub(j)

    def _stub(self, j: dict) -> Stub:
        return Stub(
            stable_id=j["slug"],
            landing_url=j["url"] or BASE,
            title=j["title"] or j["citation"],
            court="hca",
            hint_date=_iso_date(j["date"]),
            hints=j,
        )

    # -- fetch -----------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        j = stub.hints
        cite = j.get("citation")
        # A metadata stub — the judgment page is WAF-walled, so we hold identity + the
        # verified HCA link (+ coram/date), not the text. The row HTML is the payload so
        # a re-import dedups; full text is a later enrichment when a real-Chrome fetch lands.
        row_html = f"{cite} | {j.get('title')} | {j.get('coram')}".encode("utf-8")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=j.get("title") or cite or stub.stable_id,
            court="hca",
            decision_date=_iso_date(j.get("date")),
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=row_html,
            raw_ext="html",
            text=None,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["au-caselaw", "commonwealth", "hca"],
            extra={k: v for k, v in {
                "jurisdiction": "commonwealth",
                "neutral_citation": cite,
                "coram": j.get("coram"),
                "url": stub.landing_url,
                "metadata_only": True,           # no body text held (view on HCA)
                "needs_fetch": True,             # full text pending a real-Chrome fetch
                "aliases": [cite.casefold()] if cite else None,
            }.items() if v not in (None, [], "")},
        )


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _hca_date(text: str | None) -> date | None:
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})", (text or "").strip())
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    ym = re.search(r"(\d{4})", text or "")
    try:
        return date(int(ym.group(1)), _MONTHS[m.group(2).lower()], int(m.group(1))) if ym else None
    except ValueError:
        return None


def _iso_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def _year_range(years: str | None) -> list[int]:
    now = datetime.now().year
    if not years or years.lower() == "current":
        return [now]
    if years.lower() == "all":
        return list(range(now, _FIRST_YEAR - 1, -1))
    m = re.match(r"(\d{4})\s*-\s*(\d{4})", years)
    if m:
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        return list(range(hi, lo - 1, -1))
    return [int(y) for y in re.findall(r"\d{4}", years)] or [now]
