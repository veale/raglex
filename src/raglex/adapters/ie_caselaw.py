"""Courts Service of Ireland judgments — the ww2.courts.ie register (§1.5).

The Courts Service publishes every senior-court judgment as a PDF behind a Drupal/Alfresco
site. Discovery has two surfaces, both served over ordinary HTTP (no JS, no proxy) and
both explicitly permitted by the site's ``robots.txt``:

1. **Keep-current** — the ``/judgments`` landing listing, newest-uploaded first. A run
   walks it until it meets a judgment already held (matched on the site's own stable
   *case-folder* UUID) and stops. One page most days.
2. **Backfill** — the ``/search/judgments-year/…`` faceted search, which (unlike the
   landing page's three-page cap) exposes the full register back to 2001, one
   ``(court, year)`` at a time, paginated. Courts covered: Supreme Court, Court of Appeal,
   High Court.

Each listing row links to a ``/view/judgments/{doc}/{case}/{file}`` **detail page** whose
labelled block is the authoritative metadata — ``Neutral Citation``, ``Record Number``,
``Court``, ``Judgment By``, ``Date Delivered``, ``Status`` — plus the direct
``/acc/alfresco/…/*.pdf/pdf`` link. Identity is taken from that block, **never** the PDF
filename, which is unreliable (``2025_IESC_31_.pdf`` on the site actually holds
"[2026] IESC 31"; others are freeform "``IESC 30.2026 BOI v Murray…``").

**Multi-opinion cases.** A single neutral citation ("[2025] IESC 49") can be delivered as
several PDFs — one per judge (majority, concurring, dissenting) — which the site groups by
a shared *case-folder* UUID (the second path segment). We store each opinion as its own
document (flat, judge-attributed), grouped by the shared ``case_citation`` for the UI. One
opinion per case-folder — the *lead*, chosen deterministically as the lexicographically
smallest document UUID — is keyed by the bare neutral-citation slug (``iesc/2025/49``), so
every "[2025] IESC 49" citation elsewhere in the corpus resolves onto it; the others are
keyed ``iesc/2025/49/<judge-slug>`` and point back via ``case_id``. Opinion *role*
(majority/concurring/dissenting) is left unset here — inferred later from the text.
"""

from __future__ import annotations

import html as _html
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, urljoin, urlsplit

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats.ie_courts_pdf import parse_ie_pdf

BASE_URL = "https://ww2.courts.ie"
LANDING_URL = f"{BASE_URL}/judgments"
YEAR_SEARCH = f"{BASE_URL}/search/judgments-year/"

# A polite floor for a small government service. The rate-limited client widens this
# automatically on a 429/503 (§1.8).
_MIN_INTERVAL = 1.5

# The three senior courts we backfill, as they appear in the ``alfresco_Court`` facet.
BACKFILL_COURTS: tuple[str, ...] = ("Supreme Court", "Court of Appeal", "High Court")
_EARLIEST_YEAR = 2001

# Map the site's court label → the neutral-citation slug head (and our court bucket).
_COURT_SLUG = {
    "supreme court": "iesc",
    "court of appeal": "ieca",
    "high court": "iehc",
    "court of criminal appeal": "iecca",
    "circuit court": "iecc",
    "district court": "iedc",
}

# "[2026] IEHC 509" / "2026 IEHC 509" → the parts of the slug, tolerant of the punctuation
# and stray spaces the Neutral Citation field carries.
_CITE_RE = re.compile(
    r"\[?\s*(?P<year>(?:19|20)\d{2})\s*\]?\s*(?P<court>IE[A-Z]+)\s*(?P<num>\d+)", re.I)

# One listing row and its cells (both the landing and year-search tables share the shape).
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_VIEW_RE = re.compile(r"href='(/view/judgments[^']*)'")
_ACC_RE = re.compile(r'href="(/acc/alfresco/[^"#]+)')
# The detail page's labelled metadata block: <span class="cell-title">Label</span>Value…
_FIELD_RE = re.compile(r'<span class="cell-title">([^<]+)</span>(.*?)</div>', re.S)
_PAGES_RE = re.compile(r"Page 1 of\s*(\d+)")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


# ── identity ─────────────────────────────────────────────────────────────────
def ie_case_slug(citation: str | None) -> str | None:
    """"[2025] IESC 49" → ``iesc/2025/49`` — the same slug the citation grammar mints, so
    a stored judgment is the node every "[2025] IESC 49" reference resolves to. None when
    the string carries no Irish neutral citation."""
    m = _CITE_RE.search(citation or "")
    if not m:
        return None
    return f"{m.group('court').lower()}/{m.group('year')}/{int(m.group('num'))}"


def filename_slug(filename: str | None) -> str | None:
    """Best-effort neutral-cite slug from the PDF filename ("2026_IEHC_509.pdf" →
    ``iehc/2026/509``) — the only citation signal available at *discovery* time, before
    the detail page is fetched. Used ONLY to pre-filter cases already held (e.g. seeded by
    a bulk import) so we don't download them again; it is never used to *label* a case.
    The filename is unreliable (a handful mis-state the year, or are freeform), so a wrong
    or absent slug just means the case isn't pre-filtered — the authoritative slug minted
    from the detail page in fetch() then catches it (the runner's provisional-id dedup)."""
    return ie_case_slug((filename or "").replace("_", " "))


def _judge_slug(judge: str | None) -> str:
    """"O'Malley J." → ``omalley-j``; "" → ``opinion``. The disambiguator for a
    non-lead opinion's id within its case."""
    slug = re.sub(r"[^a-z0-9]+", "-", (judge or "").lower()).strip("-")
    return slug or "opinion"


def _court_bucket(court_label: str | None, slug: str | None) -> str:
    if court_label and (b := _COURT_SLUG.get(court_label.strip().lower())):
        return b
    return (slug or "iehc").split("/", 1)[0]


# ── row / detail parsing (pure) ──────────────────────────────────────────────
def parse_listing(html: str) -> list[dict]:
    """A listing page (landing or year-search) → one dict per row, in document order.
    Carries the view/PDF URLs and the site's two path UUIDs (doc + case-folder)."""
    body = html.split("<tbody>", 1)[-1].split("</tbody>", 1)[0] if "<tbody>" in html else ""
    out: list[dict] = []
    for m in _ROW_RE.finditer(body):
        row = m.group(1)
        cells = _CELL_RE.findall(row)
        if len(cells) < 4:
            continue
        view = _VIEW_RE.search(row)
        acc = _ACC_RE.search(row)
        if not view:
            continue
        view_path = _html.unescape(view.group(1))
        parts = view_path.split("/")  # ['', 'view', 'judgments[-year]', DOC, CASE, FILE, 'pdf']
        # year-search rows drop the Title's own column layout by one (no separate pdf cell);
        # detect by cell count: landing has [date,title,pdf,court,judge,uploaded];
        # year-search has [date,title,pdf?,judge,uploaded]. Court comes from the metadata
        # on fetch either way, so we don't rely on the cell here.
        out.append({
            "date": _text(cells[0]),          # date delivered (→ decision_date, display)
            "title": _text(cells[1]),
            # date uploaded is the last cell on both the landing and year-search tables. It
            # is the landing page's sort key (newest-uploaded first) and the ONLY monotonic
            # cursor: a judgment delivered in 2024 can be uploaded today, so the delivered
            # date is not usable as an incremental watermark.
            "uploaded": _text(cells[-1]),
            "view_path": view_path,
            "acc_path": _html.unescape(acc.group(1)) if acc else None,
            "doc_uuid": parts[3] if len(parts) > 4 else None,
            "case_uuid": parts[4] if len(parts) > 5 else None,
            "filename": parts[5] if len(parts) > 5 else None,
        })
    return out


def parse_detail(html: str) -> dict:
    """A ``/view/judgments/…`` detail page → its authoritative metadata block + PDF link."""
    meta = {_text(k).lower(): _text(v) for k, v in _FIELD_RE.findall(html)}
    pdf = re.search(r"href='(/acc/alfresco/[^'#]+)'", html)
    return {
        "citation": meta.get("neutral citation"),
        "record_number": meta.get("record number"),
        "court": meta.get("court"),
        "judge": meta.get("judgment by"),
        "date": meta.get("date delivered"),
        "status": meta.get("status"),
        "pdf_path": _html.unescape(pdf.group(1)) if pdf else None,
    }


def _lead_case_uuids(rows: list[dict]) -> dict[str, str]:
    """For each case-folder UUID, the lead document's UUID = the lexicographically smallest
    doc UUID in the group. Deterministic and stable across runs, so the same opinion always
    owns the bare-citation slug (§ identity). Rows with no case UUID are their own lead."""
    groups: dict[str, list[str]] = {}
    for r in rows:
        if r["case_uuid"] and r["doc_uuid"]:
            groups.setdefault(r["case_uuid"], []).append(r["doc_uuid"])
    return {case: min(docs) for case, docs in groups.items()}


# ── date parsing ─────────────────────────────────────────────────────────────
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def _parse_date(s: str | None) -> date | None:
    """Both listing ("22/07/2026") and detail ("22 July 2026") date forms → a date."""
    s = (s or "").strip()
    if m := re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s):
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    if m := re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s):
        mon = _MONTHS.get(m.group(2).lower())
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                return None
    return None


def _clean_title(title: str) -> str:
    """The listing title is the party line; normalise the "-v-" the site wraps in dashes to
    a plain " v " so it matches how the corpus writes party names."""
    t = re.sub(r"\s*-\s*v\s*-\s*", " v ", title, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", t).strip()


class IrishCaseLawAdapter(BaseAdapter):
    source = "ie-caselaw"
    min_interval = _MIN_INTERVAL
    requires_js = False
    requires_proxy = False

    def __init__(self, *, client: RateLimitedClient | None = None,
                 path: str | None = None, courts: tuple[str, ...] = BACKFILL_COURTS,
                 earliest_year: int = _EARLIEST_YEAR) -> None:
        self.path = Path(path) if path else None
        self.courts = courts
        self.earliest_year = earliest_year
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=60)

    # -- discover ----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is not None:
            yield from self._discover_saved()
            return
        if since:
            # Keep-current: the landing page, newest first, stopping at the first row whose
            # case-folder is already held (its UUID <= the stored watermark's).
            yield from self._discover_landing(since)
        else:
            # Backfill: every (court, year) from this year back to the earliest.
            yield from self._discover_backfill(max_pages=max_pages)

    # The landing listing is sorted newest-uploaded first and (unlike the year search)
    # capped at a few pages — enough for a routine keep-current pass. We stop the moment we
    # cross the watermark, so a normal run reads a single page and exits.
    _LANDING_MAX_PAGES = 3

    def _discover_landing(self, since: str) -> Iterator[Stub]:
        """Keep-current: walk the landing page (newest-uploaded first) and stop as soon as a
        row's upload date falls at or before the watermark — everything past it is older and
        already held. Normally exits on page 1 without touching the year search at all."""
        seen_docs: set[str] = set()
        for page in range(self._LANDING_MAX_PAGES):
            url = LANDING_URL if page == 0 else f"{LANDING_URL}?page={page}"
            try:
                html = self._client.get(url).text
            except FetchError:
                return
            rows = parse_listing(html)
            if not rows:
                return
            leads = _lead_case_uuids(rows)
            for r in rows:
                uploaded = _parse_date(r.get("uploaded"))
                # Sorted by upload desc: once a row is at/below the cursor, so is the rest.
                if uploaded and uploaded.isoformat() <= since:
                    return
                if r["doc_uuid"] in seen_docs:
                    continue
                seen_docs.add(r["doc_uuid"])
                for stub in self._case_stubs(r, rows, leads):
                    yield stub

    def _discover_backfill(self, *, max_pages: int | None) -> Iterator[Stub]:
        this_year = datetime.now().year
        yielded_pages = 0
        seen_docs: set[str] = set()
        for court in self.courts:
            for year in range(this_year, self.earliest_year - 1, -1):
                base = YEAR_SEARCH + quote(self._year_query(year, court), safe="")
                try:
                    html = self._client.get(base).text
                except FetchError:
                    continue
                pm = _PAGES_RE.search(html)
                npages = int(pm.group(1)) if pm else 1
                page_htmls = [html] + [
                    self._safe_get(f"{base}?page={p}") for p in range(1, npages)]
                page_rows = [r for h in page_htmls if h for r in parse_listing(h)]
                leads = _lead_case_uuids(page_rows)
                for r in page_rows:
                    if r["doc_uuid"] in seen_docs:
                        continue
                    seen_docs.add(r["doc_uuid"])
                    for stub in self._case_stubs(r, page_rows, leads):
                        yield stub
                yielded_pages += npages
                if max_pages is not None and yielded_pages >= max_pages:
                    return

    def _discover_saved(self) -> Iterator[Stub]:
        files = ([self.path] if self.path.is_file()
                 else sorted(self.path.rglob("*.html")) if self.path.is_dir() else [])
        seen_docs: set[str] = set()
        for fp in files:
            try:
                html = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rows = parse_listing(html)
            leads = _lead_case_uuids(rows)
            for r in rows:
                if r["doc_uuid"] in seen_docs:
                    continue
                seen_docs.add(r["doc_uuid"])
                for stub in self._case_stubs(r, rows, leads):
                    yield stub

    @staticmethod
    def _year_query(year: int, court: str) -> str:
        return ('" type:Judgment" AND "filter:alfresco_year.true" AND '
                f'"filter:alfresco_todate.{year}" AND "filter:alfresco_Court.{court}"')

    def _safe_get(self, url: str) -> str | None:
        try:
            return self._client.get(url).text
        except FetchError:
            return None

    def _case_stubs(self, row: dict, siblings: list[dict], leads: dict[str, str]) -> Iterator[Stub]:
        """One stub for this listing row, carrying whether it's the lead opinion of its
        case-folder and how many siblings it has."""
        case = row["case_uuid"]
        is_lead = (case is None) or (leads.get(case) == row["doc_uuid"])
        sib_count = sum(1 for s in siblings if s["case_uuid"] == case) if case else 1
        # The detail page uses /view/judgments/ (year-search rows use /view/judgments-year/,
        # which also serves the metadata, but normalise to the canonical path).
        view = row["view_path"].replace("/view/judgments-year/", "/view/judgments/")
        # Provisional id = the best-effort slug from the filename (so the prefilter skips a
        # case already held under that citation — e.g. from a bulk import — WITHOUT fetching
        # it). It is not used to label the case: fetch() mints the authoritative id from the
        # detail page. A wrong/absent filename slug just forgoes the pre-filter; the runner's
        # provisional-id dedup then catches it once the real id is known.
        provisional = filename_slug(row.get("filename")) or f"ie-caselaw/{row['doc_uuid']}"
        # For a non-lead opinion, namespace the provisional id by its file so it can't be
        # mistaken for the lead's citation (the lead owns the bare slug; see fetch()).
        if not is_lead and "/" in provisional:
            provisional = f"{provisional}/{row['doc_uuid'][:8]}"
        # Monotonic watermark: the upload date (the landing page's sort key). The delivered
        # date is not usable — an old judgment can be uploaded today.
        uploaded = _parse_date(row.get("uploaded"))
        yield Stub(
            stable_id=provisional,
            title=_clean_title(row["title"]) if row["title"] else None,
            landing_url=urljoin(BASE_URL, _url_quote(view)),
            hint_date=_parse_date(row["date"]),
            hints={
                "case_uuid": case, "doc_uuid": row["doc_uuid"],
                "is_lead": is_lead, "sibling_count": sib_count,
                "acc_path": row["acc_path"], "listing_title": row["title"],
                "watermark": uploaded.isoformat() if uploaded else None,
            },
        )

    # -- fetch -------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        try:
            page = self._client.get(stub.landing_url).text
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        meta = parse_detail(page)
        cite = meta.get("citation")
        slug = ie_case_slug(cite)
        pdf_path = meta.get("pdf_path") or stub.hints.get("acc_path")
        if not slug or not pdf_path:
            return None  # a row we can't key or can't reach — a genuine absence

        court = _court_bucket(meta.get("court"), slug)
        # Identity: the lead opinion owns the bare citation slug (the resolution target);
        # a non-lead opinion is namespaced by its judge so it can't collide with the lead.
        is_lead = stub.hints.get("is_lead", True)
        stable_id = slug if is_lead else f"{slug}/{_judge_slug(meta.get('judge'))}"

        pdf_url = urljoin(BASE_URL, _url_quote(pdf_path))
        try:
            raw = self._client.get(pdf_url).content
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        parsed = parse_ie_pdf(raw)
        if not (parsed.text or "").strip() and not parsed.needs_ocr:
            return None

        title = stub.title or _clean_title(stub.hints.get("listing_title") or "")
        dec_date = _parse_date(meta.get("date")) or stub.hint_date

        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.JUDGMENT,
            title=title or cite or stable_id,
            court=court,
            decision_date=dec_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=parsed.text or None,
            segments=parsed.segments,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["ie-caselaw", "ireland", court],
            extra={k: v for k, v in {
                "jurisdiction": "ie",
                "neutral_citation": cite,
                "case_citation": cite,            # UI grouping key across sibling opinions
                "case_id": slug,                  # every opinion of a case shares this
                "judge": meta.get("judge"),
                "role": None,                     # majority/concurring/dissenting — inferred later
                "record_number": meta.get("record_number"),
                "status": meta.get("status"),
                "is_lead_opinion": is_lead,
                "sibling_opinions": (stub.hints.get("sibling_count") or 1) - 1 or None,
                "pdf_url": pdf_url,
                "needs_ocr": parsed.needs_ocr or None,
            }.items() if v not in (None, "", [])},
        )


def _url_quote(path: str) -> str:
    """Percent-encode a site path's segments (filenames carry spaces, brackets, ``&``)
    while leaving the ``/`` separators and any existing ``%`` escapes intact."""
    split = urlsplit(path)
    encoded = "/".join(quote(seg, safe="%") for seg in split.path.split("/"))
    return encoded + (f"#{split.fragment}" if split.fragment else "")
