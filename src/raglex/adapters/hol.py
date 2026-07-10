"""House of Lords judgments — publications.parliament.uk (§5a, scraping tier).

The Appellate Committee's judgments (1996–2009, until the UKSC took over) live only as
legacy table-based HTML on publications.parliament.uk — no API, Cloudflare bot
protection, and pre-2001 cases have **no neutral citation at all**, so they're cited only
by law report ("[1998] AC 1"). This adapter harvests them so that:

  * post-2001 cases resolve every "[YYYY] UKHL N" citation (keyed ``ukhl/YYYY/N``);
  * pre-2001 cases become real documents (keyed by a ``hol/...`` surrogate) that the
    report-citation matcher (citations.report_match) can link a "[1998] AC 1" to.

Fetching goes through the scraping tier's stealth fetcher (Scrapling / Camoufox, or a
scrapling-MCP service) because a plain request is bot-blocked. Parsing is bespoke — the
site is one giant year-anchored table, and judgments paginate via "Continue" links — so
this adapter owns its own HTML parsers (unit-tested against the documented structure).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urljoin

from ..core.adapter import BaseAdapter
from ..core.models import DocType, ExtractedVia, Record, Stub, sha256_bytes
from ..scraping.fetcher import Fetcher, get_fetcher

INDEX_URL = "https://publications.parliament.uk/pa/ld/ldjudgmt.htm"

_NCN_RE = re.compile(r"\[(?P<year>\d{4})\]\s*UKHL\s*(?P<num>\d+)", re.IGNORECASE)
# front-matter paragraph classes to drop (cover page / counsel / nav), keeping prose
_FRONT_MATTER = {"bigcov", "medcov", "smcov"}
# the site's silent-failure page (served with HTTP 200) when you walk past the last page
_SOFT_404 = re.compile(r"Page cannot be found", re.IGNORECASE)


@dataclass(slots=True)
class HolCase:
    title: str
    citation: str | None   # "[2009] UKHL 5" or None (pre-2001)
    date: str | None       # "17 June 2009"
    url: str               # absolute page-1 URL

    @property
    def stable_id(self) -> str:
        """``ukhl/YYYY/N`` when a neutral citation exists (so "[YYYY] UKHL N" edges
        resolve), else a ``hol/<session>/<stem>`` surrogate derived from the URL."""
        if self.citation:
            m = _NCN_RE.search(self.citation)
            if m:
                return f"ukhl/{m.group('year')}/{int(m.group('num'))}"
        # surrogate from the path: /pa/ld200809/ldjudgmt/jd090617/assom.htm → hol/ld200809/assom
        parts = [p for p in self.url.split("?")[0].split("/") if p]
        session = next((p for p in parts if re.fullmatch(r"ld\d{6}", p)), None)
        stem = re.sub(r"(-1)?\.html?$", "", parts[-1]) if parts else "case"
        return f"hol/{session or 'x'}/{stem}"

    @property
    def year(self) -> int | None:
        if self.citation:
            m = _NCN_RE.search(self.citation)
            if m:
                return int(m.group("year"))
        m = re.search(r"\b(19|20)\d{2}\b", self.date or "")
        return int(m.group(0)) if m else None


def parse_index(html: str, base_url: str = INDEX_URL) -> list[HolCase]:
    """Every case row in the index table → a :class:`HolCase`. Robust to the two link
    styles (root- vs parent-relative, always ``urljoin``-ed) and to early-year tables
    that lack ``id="AutoNumber1"``: a case row is simply a ``<tr>`` whose first cell
    links to a judgment page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    cases: list[HolCase] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "ldjudgmt/" not in href or href.endswith("ldjudgmt.htm"):
            continue  # only individual-judgment links (not the index/section anchors)
        row = a.find_parent("tr")
        if row is None:
            continue
        cells = row.find_all("td")
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        citation = cells[1].get_text(" ", strip=True) if len(cells) > 1 else None
        date = cells[2].get_text(" ", strip=True) if len(cells) > 2 else None
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        cases.append(HolCase(title=title, citation=_clean_citation(citation),
                             date=date or None, url=url))
    return cases


def _clean_citation(text: str | None) -> str | None:
    if not text:
        return None
    t = text.strip()
    return t if _NCN_RE.search(t) else None  # only keep a real "[YYYY] UKHL N"


def parse_case_page(html: str) -> tuple[list[str], str | None]:
    """Extract the judgment paragraphs from one page and the **absolute-relative** href
    of a "Continue" link if the judgment paginates (else None). Returns ``([], None)`` for
    the site's soft-404. Front-matter and navigation are dropped; substantive prose kept.

    Pagination is detected by the *link text* "Continue" (never by guessing filenames),
    and the real ``href`` is returned for the caller to ``urljoin``."""
    from bs4 import BeautifulSoup

    if _SOFT_404.search(html):
        return [], None
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find(id="maincontent") or soup
    paras: list[str] = []
    for p in main.find_all("p"):
        cls = set(p.get("class") or [])
        if cls & _FRONT_MATTER:
            continue
        txt = p.get_text(" ", strip=True)
        low = txt.lower()
        if not txt or low in ("continue", "previous", "(back to preceding text)"):
            continue
        if low.startswith("©") or "parliamentary copyright" in low or low.startswith("prepared "):
            continue  # site footer, not judgment prose
        paras.append(txt)
    cont = soup.find("a", string=lambda s: s and s.strip().lower() == "continue")
    return paras, (cont["href"] if cont and cont.has_attr("href") else None)


class HouseOfLordsAdapter(BaseAdapter):
    source = "uk-hol"
    min_interval = 1.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 max_pages_per_case: int = 30, fetcher: Fetcher | None = None) -> None:
        # targeted mode: only fetch these stable_ids (from the resolver's worklist)
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = set(ids) if ids else None
        self.max_pages_per_case = max_pages_per_case
        # stealth by default (the domain is bot-protected); the engine is configurable.
        self._fetcher = fetcher or get_fetcher(source=self.source, min_interval=self.min_interval,
                                               name="stealth")

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        page = self._fetcher.fetch(INDEX_URL)
        for case in parse_index(page.html, INDEX_URL):
            if self.ids is not None and case.stable_id not in self.ids:
                continue
            yield Stub(stable_id=case.stable_id, landing_url=case.url, raw_url=case.url,
                       title=case.title, court="ukhl",
                       hints={"citation": case.citation or "", "date": case.date or "",
                              "year": str(case.year or "")})

    def fetch(self, stub: Stub) -> Record | None:
        url = stub.raw_url
        all_paras: list[str] = []
        for _ in range(self.max_pages_per_case):
            page = self._fetcher.fetch(url)
            paras, cont = parse_case_page(page.html)
            all_paras.extend(paras)
            if not cont:
                break
            url = urljoin(url, cont)
        if not all_paras:
            return None
        text = "\n\n".join(all_paras)
        year = stub.hints.get("year") or ""
        decision_date = f"{year}-01-01" if re.fullmatch(r"\d{4}", year) else None
        return Record(
            source=self.source, stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT, title=stub.title, court="ukhl",
            decision_date=_parse_date(stub.hints.get("date")) or decision_date,
            language="en", source_language="en", landing_url=stub.landing_url,
            raw_bytes=text.encode("utf-8"), raw_ext="txt", text=text,
            extracted_via=ExtractedVia.SCRAPE,
            extra={"citation": stub.hints.get("citation") or None,
                   "source_site": "publications.parliament.uk"},
        )


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def _parse_date(text: str | None) -> str | None:
    """"17 June 2009" → "2009-06-17" (the index's date format), else None."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    mon = _MONTHS.get(m.group(2).lower())
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}" if mon else None
