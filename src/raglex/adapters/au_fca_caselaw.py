"""Federal Court of Australia — live incremental case law (currency over the OALC bulk).

The companion to :mod:`au_nsw_caselaw` for the federal jurisdiction. It harvests the
Federal Court's judgments database the way the OALC creator's
``federal_court_of_australia`` scraper does — the Funnelback search at
``search.judgments.fedcourt.gov.au`` → each ``judgments.fedcourt.gov.au`` judgment page
— but **newest-first and incremental**: ``sort=date`` (descending) means page 1 holds
the latest decisions, so the crawl pages by ``start_rank`` only until it reaches the
watermark. The database spans the whole federal set the OALC pulls (FCA, the Full Court
FCAFC, and the federal tribunals IRCA/ACOMPT/ACOPYT/ADFDAT/FPDT, plus the Supreme Court
of Norfolk Island NFSC).

**Blocking.** The search backend WAFs a plain HTTP client (403), so — like GDPRhub —
every request goes through the stealth tier (which also decodes the judgments' quirky
windows-1250 encoding, since a real browser reads the page's own charset).

**Identity is shared with the bulk corpus.** A result URL carries the court/year/number
in its last path segment (``…/fca/single/2026/2026fca0981``), which maps to the same
``fca/2026/981`` slug :func:`au_case_slug` mints for "[2026] FCA 981" — so a live
decision is the same node as its OALC-snapshot copy and resolves the pending neutral-
citation references the corpus already holds. The reconstructed neutral citation is
minted as an alias too.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.models import DocType, ExtractedVia, Record, Stub

# Funnelback search, newest-first (sort=date), across every federal court/tribunal in
# the database. num_ranks/start_rank paginate; query_sand empty = everything.
_COURTS = "FCA+FCAFC+IRCA+ACOMPT+ACOPYT+ADFDAT+FPDT+NFSC"
SEARCH = (
    "https://search.judgments.fedcourt.gov.au/s/search.html"
    "?collection=fca%7Esp-judgments-internet&profile=judgments-internet"
    f"&sort=date&meta_CourtID_orsand={_COURTS}&query_sand="
)
PER_PAGE = 20

# a result row: the judgment URL + its case-name title, then the date in the meta line
_RESULT = re.compile(
    r'<a href="(https://www\.judgments\.fedcourt\.gov\.au/judgments/Judgments/[^"]+)"'
    r'\s+title="([^"]*)">')
_META = re.compile(r'<p class=meta>([^<]*)<span class="divide">')
# the citable token in the URL tail: 2026fca0981 → (2026, fca, 0981)
_SEG = re.compile(r'(\d{4})([a-z]+)(\d+)$')
# the judgment body sits in a judgment_content div (docx_judgment_content for the
# Word-sourced ones); slicing from it drops the surrounding site nav/chrome
_CONTENT_START = re.compile(r'<div\s+class="(?:docx_)?judgment_content"', re.I)


def fca_slug(url: str) -> tuple[str, str] | None:
    """A judgment URL → (stable_id slug, reconstructed neutral citation), or None.
    ``…/Judgments/fca/single/2026/2026fca0981`` → ("fca/2026/981", "[2026] FCA 981")."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    m = _SEG.match(tail)
    if not m:
        return None
    year, court, num = m.group(1), m.group(2), int(m.group(3))
    return f"{court}/{year}/{num}", f"[{year}] {court.upper()} {num}"


def parse_serp(html: str) -> list[dict]:
    """One Funnelback results page (pure) → its decisions, newest-first."""
    results = _RESULT.findall(html)
    metas = _META.findall(html)
    out: list[dict] = []
    for (url, title), longdate in zip(results, metas):
        ids = fca_slug(url)
        if not ids:
            continue
        slug, neutral = ids
        d = _fca_date(longdate)
        out.append({"url": url, "title": (title or "").strip(), "slug": slug,
                    "neutral": neutral, "date": d.isoformat() if d else None,
                    "jurisdiction": "norfolk_island" if "/nfsc/" in url else "commonwealth"})
    return out


class FCACaselawAdapter(BaseAdapter):
    source = "au-fca"
    min_interval = 3.0
    requires_js = True      # the search WAFs plain HTTP → stealth tier
    requires_proxy = False

    _MAX_PAGES_BACKFILL = 600  # ~12k decisions ceiling for a first walk

    def __init__(self, *, fetcher=None, max_pages: int | None = None) -> None:
        self._fetcher = fetcher
        self._max_pages_cfg = max_pages

    def _get(self):
        if self._fetcher is None:
            from ..scraping.fetcher import get_fetcher
            self._fetcher = get_fetcher("stealth", source=self.source,
                                        min_interval=self.min_interval, requires_js=True)
        return self._fetcher

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        cap = max_pages or self._max_pages_cfg or (None if since else self._MAX_PAGES_BACKFILL)
        seen: set[str] = set()
        page = 0
        while True:
            start = page * PER_PAGE + 1
            try:
                html = self._get().fetch(f"{SEARCH}&num_ranks={PER_PAGE}&start_rank={start}").html or ""
            except FetchError:
                return
            rows = parse_serp(html)
            if not rows:
                return
            reached = False
            progressed = False
            for r in rows:
                if r["slug"] in seen:      # the database duplicates some SERPs — dedup
                    continue
                seen.add(r["slug"])
                progressed = True
                if since and r["date"] and r["date"] <= since:
                    reached = True
                    break
                yield Stub(
                    stable_id=r["slug"],
                    landing_url=r["url"],
                    title=r["title"] or r["neutral"],
                    court=r["slug"].split("/")[0],
                    hint_date=_iso_date(r["date"]),
                    hints={"neutral": r["neutral"], "date": r["date"],
                           "jurisdiction": r["jurisdiction"],
                           **({"watermark": r["date"]} if r["date"] else {})},
                )
            page += 1
            if reached or not progressed or (cap is not None and page >= cap):
                return

    def fetch(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        try:
            html = self._get().fetch(stub.landing_url).html or ""
        except FetchError:
            return None
        # extract the judgment body, not the surrounding site chrome
        m = _CONTENT_START.search(html)
        fragment = html[m.start():] if m else html
        text = extract_bytes(fragment.encode("utf-8"), ext="html", mime="text/html").text
        if not text:
            return None
        neutral = stub.hints.get("neutral")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=stub.title or neutral or stub.stable_id,
            court=stub.court or stub.stable_id.split("/")[0],
            decision_date=_iso_date(stub.hints.get("date")),
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=html.encode("utf-8"),
            raw_ext="html",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["au-caselaw", stub.hints.get("jurisdiction") or "commonwealth"],
            extra={k: v for k, v in {
                "jurisdiction": stub.hints.get("jurisdiction") or "commonwealth",
                "neutral_citation": neutral,
                "url": stub.landing_url,
                "aliases": [neutral.casefold()] if neutral else None,
            }.items() if v not in (None, [], "")},
        )


def _fca_date(longdate: str) -> date | None:
    try:
        d = datetime.strptime((longdate or "").strip(), "%d %b %Y").date()
    except ValueError:
        return None
    return d if d.year >= 1976 else None   # FCA founded 1976; earlier = bad OCR'd date


def _iso_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None
