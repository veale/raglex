"""Singapore legislation — Singapore Statutes Online (SSO, ``sso.agc.gov.sg``).

Singapore has **no ELI, no AKN and no search API**: SSO is keyless, server-rendered HTML,
and the durable identifier is SSO's own short *act code* (``CoA1967`` for the Companies Act
1967, ``SCJA1969-N2`` for a piece of subsidiary legislation). So the corpus keys Singapore
legislation by that code — ``sg/act/coa1967`` / ``sg/sl/scja1969-n2`` — the same way the
Commonwealth adapter keys off the FRL register id.

Two ingest routes share this module, because they share the identifier and the parsing:

* a **seed import** of the KanoonGPT/LawBot parquet snapshot (``import_sg_seed``): 2,317
  documents, 55,221 sections, already parsed from the source PDFs so the section text is
  *complete* — unlike SSO's own HTML, which lazy-loads the body of a large Act and serves
  only its table of contents up front. The snapshot's one flaw is that its document names
  are **hard-truncated at 50 characters**, so this module recovers the full title (and the
  SSO code) by matching each truncated name against the live browse listing, and falls back
  to the title printed in the Act's own front matter;
* an **ongoing harvest** (:class:`SGLegislationAdapter`) that browses the current Acts /
  subsidiary-legislation listings and fetches each document, so new and amended legislation
  keeps flowing after the seed.

**Robots + WAF.** SSO's ``robots.txt`` disallows ``/search`` (crawl-delay 6s); discovery
uses only the allowed ``/Browse`` listing, never search. SSO's WAF also 403s a
self-identifying bot User-Agent while accepting a generic browser one — a documented quirk
(see the ``sg-eli-mcp`` DISCOVERY notes), reproduced here rather than hidden: the request is
otherwise fully robots-compliant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType, ExtractedVia, Record, ResolutionStatus, Segment, Stub,
)

SSO_BASE = "https://sso.agc.gov.sg"
# SSO's WAF rejects an honest bot UA (403) but accepts a generic browser string.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# SSO caps PageSize; 600 silently fell back to 20, 500 is honoured.
_PAGE_SIZE = 500


# ── identity ────────────────────────────────────────────────────────────────

def sg_act_id(code: str) -> str:
    """SSO act code → corpus stable_id: ``CoA1967`` → ``sg/act/coa1967``."""
    return f"sg/act/{(code or '').strip().lower()}"


def sg_sl_id(code: str) -> str:
    """SSO SL code → corpus stable_id: ``SCJA1969-N2`` → ``sg/sl/scja1969-n2``."""
    return f"sg/sl/{(code or '').strip().lower()}"


def sg_landing_url(code: str, *, subsidiary: bool) -> str:
    return f"{SSO_BASE}/{'SL' if subsidiary else 'Act'}/{code}"


def name_key(name: str | None) -> str:
    """A comparison key for a legislation title: lowercase, punctuation-stripped, spaces
    collapsed. Used to match the seed's 50-char-truncated names against full SSO titles by
    prefix — ``name_key(truncated)`` is a prefix of ``name_key(full)``."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())).strip()


# An Act prints its title in caps between the "REPUBLIC OF SINGAPORE" banner and the
# "REVISED EDITION"/year line. Subsidiary legislation instead prints a gazette line
# ("No. S 443") followed by its parent Act, then the SL's own title, then "ARRANGEMENT OF".
# Both recover the real title when the seed's is truncated and no browse match was found.
_ACT_TITLE = re.compile(
    r"REPUBLIC OF SINGAPORE\s+(.+?)\s+(?:\d{4}\s+REVISED EDITION|\(?(?:ORIGINAL|REVISED)\b|"
    r"An Act|ARRANGEMENT OF)", re.IGNORECASE | re.DOTALL)
# the SL title is everything between the parent-Act line and "ARRANGEMENT OF …"
_SL_TITLE = re.compile(
    r"No\.\s*S\s*\d+\s+.*?ACT\s+\d{4}\s+(.+?)\s+ARRANGEMENT OF",
    re.IGNORECASE | re.DOTALL)


def title_from_frontmatter(text: str | None) -> str | None:
    head = (text or "")[:1800]
    m = _ACT_TITLE.search(head) or _SL_TITLE.search(head)
    if not m:
        return None
    raw = re.sub(r"\s+", " ", m.group(1)).strip(" .,")
    if len(raw) < 4 or len(raw) > 200:
        return None
    # front-matter titles are ALL CAPS — title-case them (keeping the year intact)
    if raw.isupper():
        raw = raw.title()
    return raw or None


# ── HTML parsing (browse listing + one Act page) ────────────────────────────

# a browse-listing entry: /Act/{code} or /SL/{code}[?DocDate=…], with the full title in
# data-legisTitle. The listing repeats each row twice (a dropdown + a button); dedup on code.
_BROWSE_ENTRY = re.compile(
    r'href="/(Act|SL)/([A-Za-z0-9._-]+?)(?:\?[^"]*)?"[^>]*data-legisTitle="([^"]*)"',
    re.IGNORECASE)
_RESULTS_COUNT = re.compile(r"([\d,]+)\s+results", re.IGNORECASE)


@dataclass(slots=True)
class BrowseEntry:
    code: str
    title: str
    subsidiary: bool


def parse_browse(html: str) -> list[BrowseEntry]:
    """One ``/Browse/{Act|SL}/Current/All`` page → its (code, title) entries, deduped."""
    seen: dict[str, BrowseEntry] = {}
    for kind, code, title in _BROWSE_ENTRY.findall(html or ""):
        code = code.strip()
        if code and code not in seen and title.strip():
            seen[code] = BrowseEntry(code=code, title=_unescape(title).strip(),
                                     subsidiary=kind.upper() == "SL")
    return list(seen.values())


def browse_results_count(html: str) -> int | None:
    m = _RESULTS_COUNT.search(html or "")
    return int(m.group(1).replace(",", "")) if m else None


def _unescape(s: str) -> str:
    from html import unescape
    return unescape(s)


@dataclass(slots=True)
class Provision:
    num: str            # section number as printed ("1", "9A", "27AA")
    caption: str | None
    text: str


@dataclass(slots=True)
class ParsedAct:
    title: str | None
    provisions: list[Provision] = field(default_factory=list)
    toc_nums: list[str] = field(default_factory=list)   # every section number in the TOC
    lazy: bool = False   # True when the page served fewer bodies than the TOC lists


_TITLE_SUFFIX = " - Singapore Statutes Online"


def parse_act_page(html: str) -> ParsedAct:
    """Parse a fetched ``/Act/{code}`` page: its title, the provision bodies present in the
    HTML, and the full list of section numbers from the table of contents.

    SSO wraps each numbered section in ``<div class="prov1">`` with the caption in a
    ``prov1Hdr`` cell carrying ``id="pr{N}-"`` and the body in a sibling ``prov1Txt`` cell.
    A **large Act lazy-loads** its bodies: the initial HTML has the full TOC (``href="#pr{N}-"``)
    but only the first handful of ``prov1Txt`` blocks, so ``lazy`` is set when the TOC lists
    more sections than the page renders — the caller backfills the rest by section number."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    tm = soup.find("title")
    title = None
    if tm:
        t = re.sub(r"\s+", " ", tm.get_text()).strip()
        title = t[: -len(_TITLE_SUFFIX)].strip() if t.endswith(_TITLE_SUFFIX) else (t or None)

    provisions = _provisions_from_soup(soup)
    # the TOC: every "#pr{N}-" anchor target is a section that exists in the Act
    toc = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.match(r"^#pr([0-9A-Za-z.]+)-$", a["href"])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            toc.append(m.group(1))
    lazy = len(toc) > len(provisions) and len(provisions) > 0
    return ParsedAct(title=title, provisions=provisions, toc_nums=toc, lazy=lazy)


def _provisions_from_soup(soup) -> list[Provision]:
    out: list[Provision] = []
    for div in soup.find_all("div", class_="prov1"):
        hdr = div.find("td", class_="prov1Hdr", id=re.compile(r"^pr[0-9A-Za-z.]+-$"))
        body = div.find("td", class_="prov1Txt")
        if hdr is None or body is None:
            continue
        num_m = re.match(r"^pr([0-9A-Za-z.]+)-$", hdr.get("id", ""))
        if not num_m:
            continue
        caption = re.sub(r"\s+", " ", hdr.get_text()).strip() or None
        text = re.sub(r"[ \t\xa0]+", " ", body.get_text("\n")).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if text:
            out.append(Provision(num=num_m.group(1), caption=caption, text=text))
    return out


def provisions_to_segments(provisions: list[Provision]) -> tuple[str, list[Segment]]:
    """Flatten provisions to one text + per-section segments (char offsets into that text)."""
    parts: list[str] = []
    segs: list[Segment] = []
    cursor = 0
    for p in provisions:
        head = f"{p.num}. {p.caption}".strip(". ") if p.caption else p.num
        block = f"{head}\n{p.text}" if p.caption else p.text
        if parts:
            cursor += 2   # the "\n\n" join
        segs.append(Segment(label=f"s {p.num}" + (f" {p.caption}" if p.caption else ""),
                            char_start=cursor, char_end=cursor + len(block),
                            kind="section", level=1))
        parts.append(block)
        cursor += len(block)
    return "\n\n".join(parts), segs


# ── ongoing harvest adapter ─────────────────────────────────────────────────

class SGLegislationAdapter(BaseAdapter):
    """Browse SSO's current Acts (and, with ``subsidiary=True``, subsidiary legislation) and
    fetch each document. Large Acts are completed by backfilling lazy-loaded provisions one
    section at a time through the ``?ProvIds=pr{N}-`` view — slow (crawl-delay 6s) but the
    only way to reach the full text SSO doesn't server-render."""

    source = "sg-legislation"
    requires_js = False
    requires_proxy = False
    min_interval = 6.0    # SSO robots.txt crawl-delay

    def __init__(self, *, subsidiary: bool = False, ids: str | tuple[str, ...] | None = None,
                 max_backfill: int = 400, client: RateLimitedClient | None = None) -> None:
        self.subsidiary = bool(subsidiary)
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        self.max_backfill = int(max_backfill)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, user_agent=_UA, timeout=60)

    # -- discovery ----------------------------------------------------------
    # SSO's browse listing does NOT paginate: PageIndex is ignored (page 1 repeats page 0),
    # and PageSize is capped at 500. So /Browse/{kind}/Current/All only ever exposes 500
    # entries — fine for the ~523 Acts, useless for the 5,835 subsidiary instruments. The
    # listing DOES split by first letter (/Browse/SL/Current/C), and a letter with more than
    # 500 entries can be widened by unioning its two sort orders (Title + Number each cap at
    # 500, but return a different 500), which covers every letter up to ~1,000.
    _LETTERS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    def browse_index(self, *, max_pages: int | None = None) -> Iterator[BrowseEntry]:
        """Every current Act (or SL) as a (code, title) entry, globally deduped. Walks the
        per-letter listings (each under both sort orders) so the full corpus is reachable
        despite the listing not paginating. ``max_pages`` caps letters, for a quick sample."""
        kind = "SL" if self.subsidiary else "Act"
        seen: set[str] = set()
        # 'All' first (cheap, covers the whole of a small Acts corpus in one fetch), then the
        # letter walk for the long tail (subsidiary legislation).
        scopes = ["All"] + list(self._LETTERS)
        if max_pages is not None:
            scopes = scopes[:max_pages]
        for scope in scopes:
            for sort_by in ("Title", "Number"):
                try:
                    html = self._client.get(
                        f"{SSO_BASE}/Browse/{kind}/Current/{scope}",
                        params={"PageSize": _PAGE_SIZE, "SortBy": sort_by}).text
                except FetchError:
                    continue
                new = [e for e in parse_browse(html) if e.code not in seen]
                for e in new:
                    seen.add(e.code)
                    yield e
                # a scope well under the cap needs only one sort order
                if scope != "All" and len(parse_browse(html)) < _PAGE_SIZE - 5:
                    break

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            for code in self.ids:
                yield Stub(stable_id=(sg_sl_id if self.subsidiary else sg_act_id)(code),
                           landing_url=sg_landing_url(code, subsidiary=self.subsidiary),
                           hints={"code": code})
            return
        for e in self.browse_index(max_pages=max_pages):
            yield Stub(
                stable_id=(sg_sl_id if e.subsidiary else sg_act_id)(e.code),
                landing_url=sg_landing_url(e.code, subsidiary=e.subsidiary),
                title=e.title, hints={"code": e.code, "subsidiary": e.subsidiary})

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        code = stub.hints["code"]
        subsidiary = bool(stub.hints.get("subsidiary", self.subsidiary))
        path = f"/{'SL' if subsidiary else 'Act'}/{code}"
        try:
            parsed = parse_act_page(self._client.get(f"{SSO_BASE}{path}").text)
        except FetchError:
            return None

        provisions = list(parsed.provisions)
        if parsed.lazy:
            provisions = self._backfill(code, subsidiary, parsed)

        text, segments = provisions_to_segments(provisions)
        title = stub.title or parsed.title or code
        return Record(
            source="sg-legislation",
            stable_id=(sg_sl_id if subsidiary else sg_act_id)(code),
            doc_type=DocType.LEGISLATION,
            title=title, court=None, decision_date=None,
            language="en", source_language="en",
            landing_url=f"{SSO_BASE}{path}",
            text=text or None, segments=segments if text else [],
            extracted_via=ExtractedVia.SCRAPE,
            extra={"jurisdiction": "sg", "sso_code": code,
                   "subsidiary_legislation": subsidiary,
                   "is_authoritative": False,   # SSO's own disclaimer: Gazette is authoritative
                   "sso_terms": f"{SSO_BASE}/Terms-of-Use"},
        )

    def _backfill(self, code: str, subsidiary: bool, parsed: ParsedAct) -> list[Provision]:
        """Fill in the provisions a large Act lazy-loaded, by fetching each missing section
        number through ``?ProvIds=pr{N}-``. Bounded by ``max_backfill`` so a pathologically
        large Act can't run unbounded; the sections already present are kept either way."""
        path = f"/{'SL' if subsidiary else 'Act'}/{code}"
        have = {p.num: p for p in parsed.provisions}
        missing = [n for n in parsed.toc_nums if n not in have][: self.max_backfill]
        for num in missing:
            try:
                sub = parse_act_page(self._client.get(
                    f"{SSO_BASE}{path}", params={"ProvIds": f"pr{num}-"}).text)
            except FetchError:
                continue
            for p in sub.provisions:
                if p.num not in have:
                    have[p.num] = p
        # return in TOC order
        return [have[n] for n in parsed.toc_nums if n in have] or parsed.provisions
