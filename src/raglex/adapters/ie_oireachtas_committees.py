"""Evidence given to Oireachtas committees — opening statements, submissions, briefings.

This is the half of Irish committee work that never reaches the Library catalogue.  A
committee's *report* is laid before the Houses and so arrives through ``ie-oireachtas``
with its date laid and its enabling provision; the evidence the committee heard is not
laid, is catalogued nowhere, and exists only as a PDF under ``data.oireachtas.ie``.  It
is also where the legal argument is: sampled against the same probe, 4 of 6 submissions
carried statutory references — one piece of correspondence to the Public Accounts
Committee cited 27 distinct Acts and 36 sections — while the committee minutes beside
them cited nothing at all.

**Coverage is deliberately bounded, and it is worth being blunt about why.**  The
complete index of this material is the publications search, which is behind an AWS WAF
captcha *and* disallowed by the site's own ``robots.txt`` (``Disallow: /*?`` — every
query-string URL).  Two other routes were tried and are closed: committee *meeting*
pages under ``/en/debates/`` are walled the same way, and ``data.oireachtas.ie`` refuses
bucket listing.  What remains is each committee's own page, which has no query string,
answers a plain client, and lists its most recent items — five per section for a sitting
committee, more for a concluded one.  So this source holds the recent tail per committee
and accumulates as it is re-run, rather than reaching back to 2016 in one sweep.  A
backfill cannot fix that; only time can.

Which committees exist is not scraped, though.  The open-data API enumerates every
committee that ever met — 232 across the 31st to 34th Dáil, against the 89 the website's
index will show you — so discovery walks that list and derives each committee's page from
its ``committeeCode``.  The derivation is confirmed by fetching, never asserted: the 31st
Dáil predates the current website and every one of its committees 404s, which is a fact
about the site rather than an error to retry.

Reports are excluded by default.  They are the same PDFs ``ie-oireachtas`` already holds
from the catalogue, and the shared content-hash dedup is global — so whichever source
runs first wins the row.  If that were this one, the document would keep the file's name
and lose the date laid, the DL number and the provision it was laid under.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from html import unescape
from typing import Iterator

from ..core.adapter import BaseAdapter, resume_floor, option_flag
from ..core.errors import FetchError, RateLimitException
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub

log = logging.getLogger("raglex.adapters.ie_oireachtas_committees")

API = "https://api.oireachtas.ie/v1/debates"
SITE = "https://www.oireachtas.ie"
DATA = "https://data.oireachtas.ie"

#: A page that certainly exists, used to tell "no such committee" from "we have been
#: blocked". Both answer 403 on this site, so nothing about a single response can
#: distinguish them — see :meth:`_page`.
CANARY = f"{SITE}/en/committees/"
#: How many committee pages may 403 in a row before the canary is consulted. A derived
#: slug being the other spelling is ordinary and happens twice per committee.
_MISSES_BEFORE_CANARY = 6

#: The API's own page size for the committee-meeting sweep. Discovery only needs the
#: distinct committees out of a year of meetings, so one request per year is enough.
MEETINGS_PER_YEAR = 600
#: The website launched for the 32nd Dáil; every 31st-Dáil committee page 404s. Asking
#: is cheap and the answer is stable, so the floor is a default rather than a rule.
FIRST_HOUSE_ON_THE_SITE = 32
#: Committee meetings are enumerable from here; the API holds nothing earlier.
FIRST_YEAR = 2012

#: The path segment that says what a file IS. Classifying on the URL rather than on the
#: heading above it means a page that renames its sections cannot silently reclassify
#: every document under it.
SUBMISSIONS = "submissions"
REPORTS = "reports"

DOC_TYPES = {
    # Evidence to a committee: an opening statement is a witness's argument, not the
    # committee's own conclusion. Same call as the UK committee source makes for its
    # scrutiny evidence and correspondence.
    SUBMISSIONS: DocType.NOTE,
    REPORTS: DocType.PREPARATORY,
}

READABLE_EXTENSIONS = ("pdf", "docx")

#: Prefixes the API's committee codes carry that the website's URLs do not. Longest
#: first: "joint_sub_committee_on_" must be tried before "joint_committee_on_" or the
#: shorter one never matches and the sub-committee is looked up under a wrong slug.
_CODE_PREFIXES = (
    "joint_sub_committee_on_", "joint_sub-committee_on_",
    "select_sub_committee_on_", "select_sub-committee_on_",
    "joint_committee_on_", "select_committee_on_", "committee_on_",
    "joint_committee_of_", "select_committee_of_",
)

#: www.oireachtas.ie refuses the shared RagLex User-Agent with a flat 403 — not the
#: captcha, just the header — while any browser UA is served normally. So the fix is the
#: headers alone; escalating to the browser tier for this would be a downgrade, and
#: reading the 403 as "the committee page is walled" would have silently emptied the
#: whole source. Sent with the Accept pair a browser would send alongside it.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) "
              "Gecko/20100101 Firefox/153.0")
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

_ROW_MARKER = "c-publications-list-compact__row"
_LINK = re.compile(
    r'href="(?P<url>https://data\.oireachtas\.ie/ie/oireachtas/committee/[^"]+)"'
    r'[^>]*?data-file-type="(?P<ext>[a-z0-9]+)"', re.S)
_TITLE = re.compile(r'__item-title">(?P<title>.*?)</p>', re.S)
_FILE_DATE = re.compile(r"/(?P<date>\d{4}-\d{2}-\d{2})_")


def committee_slugs(code: str) -> list[str]:
    """The website paths a committee code might live at, best guess first.

    ``joint_committee_on_justice`` is ``/en/committees/33/justice/`` — the website drops
    the "joint committee on" that the data code keeps. It does NOT drop it from
    ``committee_of_public_accounts``, which is a name rather than a prefix, so the
    unstripped form is always tried as well."""
    base = code.replace("_", "-")
    out: list[str] = []
    for prefix in _CODE_PREFIXES:
        if code.startswith(prefix):
            out.append(code[len(prefix):].replace("_", "-"))
            break
    out.append(base)
    return out


def committee_pages(house_no: str, slug: str) -> list[str]:
    """The two pages worth reading per committee, neither carrying a query string.

    ``/documents/`` is the fuller list; the landing page sometimes carries an item the
    documents page has already rolled off. Both are cheap and the union is deduped."""
    return [f"{SITE}/en/committees/{house_no}/{slug}/documents/",
            f"{SITE}/en/committees/{house_no}/{slug}/"]


def parse_file_date(url: str) -> date | None:
    """The publication date, from the filename rather than the row.

    The row prints "Tue, 8 Oct" — no year — while the file it links to is named
    ``2024-10-08_opening-statement-…``. Reading the row would date every October item to
    whatever year the reader assumed."""
    match = _FILE_DATE.search(url)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("date"), "%Y-%m-%d").date()
    except ValueError:
        return None


def file_family(url: str) -> str | None:
    """``submissions`` / ``reports`` — the path segment after the committee code."""
    match = re.search(
        r"/ie/oireachtas/committee/[^/]+/[^/]+/[^/]+/(?P<family>[^/]+)/", url)
    return match.group("family") if match else None


def stable_id(url: str) -> str | None:
    """``ie/oireachtas/committee/dail/33/joint_committee_on_justice/submissions/
    2024-10-08_opening-statement-…``.

    The file's own path is the identity: it names the house, the committee, what kind of
    document this is and the day it was published, and it is what a footnote to this
    material would point at. The year directory is dropped because the filename already
    carries the full date, and the ``_en`` language suffix with it."""
    match = re.search(r"/ie/oireachtas/committee/(?P<path>.+)$", url)
    if not match:
        return None
    parts = [p for p in match.group("path").split("/") if p]
    if len(parts) < 5:
        return None
    house, house_no, code, family = parts[:4]
    stem = parts[-1].rsplit(".", 1)[0]
    stem = re.sub(r"_(en|ga|mul)$", "", stem)
    return f"ie/oireachtas/committee/{house}/{house_no}/{code}/{family}/{stem}"


def parse_documents_page(html: str) -> list[dict]:
    """The document rows on a committee page.

    Split on the row's OPENING marker: every terminator that works for the rows in the
    middle has nothing to stop on for the last one, which is how a page silently loses
    its final record."""
    out: list[dict] = []
    for block in html.split(_ROW_MARKER)[1:]:
        link = _LINK.search(block)
        if not link:
            continue
        url = link.group("url")
        title = _TITLE.search(block)
        out.append({
            "url": url,
            "ext": (link.group("ext") or "").lower(),
            # Unescape after stripping tags, not before: the page writes names as
            # "O&#039;Sullivan", and storing the entity would put it in the title, the
            # search index and every citation of the document.
            "title": " ".join(unescape(re.sub(r"<[^>]+>", " ", title.group("title"))).split())
                     if title else None,
            "family": file_family(url),
            "date": parse_file_date(url),
        })
    return out


class OireachtasCommitteeEvidenceAdapter(BaseAdapter):
    source = "ie-oireachtas-committees"
    #: Deliberately slow. At 0.6s a sweep of every committee got the whole IP blocked
    #: two-thirds of the way through — and because a block and a missing page are the
    #: same 403 here, the run would have finished "successfully" with the last eighty
    #: committees recorded as empty. Full sweeps are ~400 pages; at this pace that is
    #: twenty minutes, which is the correct trade for a source re-read on every run.
    min_interval = 3.0

    def __init__(self, *, client: RateLimitedClient | None = None,
                 families: str | None = None, include_reports=None,
                 first_house=None, houses: str | None = None,
                 start_offset: int | str | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120,
            user_agent=BROWSER_UA)
        wanted = str(families or "").strip()
        self.families: tuple[str, ...] = tuple(
            f.strip() for f in wanted.split(",") if f.strip()
        ) if wanted and wanted != "*" else ()
        self.include_reports = option_flag(include_reports, wanted == "*")
        self.first_house = _int_or(first_house, FIRST_HOUSE_ON_THE_SITE)
        self.houses: tuple[str, ...] = tuple(
            h.strip() for h in str(houses or "").split(",") if h.strip())
        # Handed back by ``jobs`` from an interrupted run's checkpoint. An adapter
        # that reports ``resume_offset`` and cannot take it back raises TypeError
        # on resume, and the retry is filed as done — see core.adapter.resume_floor.
        # The cursor here counts committees, not documents, so it is not paged.
        self.start_offset = max(0, _int_or(start_offset, 0))
        self._walled: set[str] = set()
        self._misses = 0

    # ---- which committees exist --------------------------------------------------

    def committees(self, *, today: date | None = None) -> list[tuple[str, str, str]]:
        """``(houseCode, houseNo, committeeCode)`` for every committee that ever met.

        From the open-data API, not from the website's index: the index lists the
        current and previous Dáil only (89 committees), while the API's meeting record
        knows 232. One request per year, and only the distinct committees are kept."""
        today = today or date.today()
        seen: dict[tuple[str, str, str], None] = {}
        for year in range(FIRST_YEAR, today.year + 1):
            try:
                payload = self._client.get(API, params={
                    "chamber_type": "committee", "limit": MEETINGS_PER_YEAR,
                    "date_start": f"{year}-01-01", "date_end": f"{year}-12-31",
                }).json()
            except (FetchError, ValueError) as exc:
                log.warning("%s: committee list for %d failed: %s", self.source, year, exc)
                continue
            for result in payload.get("results") or []:
                house = ((result or {}).get("debateRecord") or {}).get("house") or {}
                code = house.get("committeeCode")
                if not code:
                    continue
                key = (str(house.get("houseCode") or ""), str(house.get("houseNo") or ""),
                       str(code))
                seen.setdefault(key, None)
        return list(seen)

    # ---- discovery ----------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None,
                 today: date | None = None) -> Iterator[Stub]:
        """Every committee's own page, newest house first.

        A full walk: these pages carry no date filter and no paging, so the sweep re-reads
        the same recent rows every run and the shared pipeline skips the ones already
        held. That is the honest shape of the source — see the module docstring on why the
        complete index is unavailable."""
        floor = _since_date(since)
        committees = self.committees(today=today)
        wanted = [c for c in committees if self._house_wanted(c[1])]
        wanted.sort(key=lambda c: (-_int_or(c[1], 0), c[2]))
        seen_pages: set[str] = set()
        seen_ids: set[str] = set()
        pages = 0
        for index, (_house, house_no, code) in enumerate(wanted):
            # Resuming: the cursor is the committee's position in this list, so a
            # run interrupted at committee 40 of 300 restarts there rather than
            # re-walking every earlier committee's document pages.
            if index < self.start_offset:
                continue
            for slug in committee_slugs(code):
                urls = [u for u in committee_pages(house_no, slug) if u not in seen_pages]
                if not urls:
                    break          # this committee's pages were read under another code
                found = False
                for url in urls:
                    if max_pages is not None and pages >= max_pages:
                        return
                    html = self._page(url)
                    seen_pages.add(url)
                    if html is None:
                        continue
                    pages += 1
                    found = True
                    for row in parse_documents_page(html):
                        stub = self._stub(row, house_no, code, floor, seen_ids)
                        if stub is None:
                            continue
                        stub.hints["resume_offset"] = index
                        stub.hints["feed_total"] = len(wanted)
                        yield stub
                if found:
                    break          # the slug resolved; don't try the other spelling
            else:
                log.info("%s: no page for %s (%s Dáil)", self.source, code, house_no)

    def _note_miss(self) -> None:
        """Count a 403, and once there have been several in a row ask the canary whether
        we are still being served at all. Raising stops the sweep; the pipeline keeps
        everything already yielded and does not advance a cursor past what was read."""
        self._misses += 1
        if self._misses < _MISSES_BEFORE_CANARY:
            return
        self._misses = 0
        try:
            canary = self._client.get(CANARY, headers=PAGE_HEADERS, raise_for_4xx=False)
        except FetchError:
            canary = None
        if canary is None or canary.status_code >= 400:
            log.warning(
                "%s: www.oireachtas.ie has stopped serving this client — a page known to "
                "exist answers %s. Stopping the sweep rather than recording the remaining "
                "committees as empty. The block is on the IP and clears on its own; if it "
                "recurs, raise min_interval (currently %.1fs).",
                self.source, getattr(canary, "status_code", "no response"),
                self.min_interval)
            # The orchestrator's own vocabulary for "the source is pushing back": it
            # pauses this source's queue, leaves the cursor un-advanced, and does not
            # fail the whole run — which is exactly right for a block that clears itself.
            raise RateLimitException(self.source)

    def _house_wanted(self, house_no: str) -> bool:
        if self.houses:
            return house_no in self.houses
        return _int_or(house_no, 0) >= self.first_house

    def _page(self, url: str) -> str | None:
        """A committee page, or None if it does not exist.

        **A missing committee and a blocked client are the same 403 here** — this site
        answers an unknown slug and a rate-limited caller identically, so no single
        response can tell them apart. A derived slug being the other spelling is
        ordinary, so misses are counted rather than acted on, and only a run of them
        asks the canary. Without that, a sweep that got blocked two-thirds through would
        finish "successfully" having recorded every remaining committee as empty.

        A 405 is the captcha — a different wall, worth saying once and never retrying."""
        try:
            response = self._client.get(url, headers=PAGE_HEADERS, raise_for_4xx=False)
        except FetchError as exc:
            log.info("%s: %s: %s", self.source, url, exc)
            return None
        if response.status_code == 405:
            host_path = url.split("?", 1)[0].rsplit("/", 2)[0]
            if host_path not in self._walled:
                self._walled.add(host_path)
                log.warning("%s: %s is behind the site's captcha — this path cannot be "
                            "read by any plain client", self.source, url)
            return None
        if response.status_code >= 400 or "data.oireachtas.ie" not in response.text:
            if response.status_code == 403:
                self._note_miss()
            return None
        self._misses = 0
        return response.text

    def _stub(self, row: dict, house_no: str, code: str, floor: date | None,
              seen: set[str]) -> Stub | None:
        family = row.get("family")
        if family not in self._families():
            return None
        if row["ext"] and row["ext"] not in READABLE_EXTENSIONS:
            return None
        key = stable_id(row["url"])
        if not key or key in seen:
            return None
        published = row.get("date")
        if floor and published and published < floor:
            return None
        seen.add(key)
        return Stub(
            stable_id=key,
            landing_url=f"{SITE}/en/committees/{house_no}/",
            raw_url=row["url"],
            title=row.get("title"),
            court="oireachtas",
            hint_date=published,
            hints={"family": family, "committee_code": code, "house_no": house_no,
                   "extension": row["ext"] or None},
        )

    def _families(self) -> tuple[str, ...]:
        if self.families:
            return self.families
        return (SUBMISSIONS, REPORTS) if self.include_reports else (SUBMISSIONS,)

    # ---- fetch --------------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        try:
            blob = self._client.get(stub.raw_url or "").content
        except FetchError as exc:
            log.info("%s: %s could not be downloaded: %s",
                     self.source, stub.stable_id, exc)
            return None
        if not blob:
            return None
        extension = self._extension(stub, blob)
        if extension is None:
            return None
        text, needs_ocr, engine = self._text(blob, extension)
        if not text and not needs_ocr:
            return None
        family = str(stub.hints.get("family") or SUBMISSIONS)
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DOC_TYPES.get(family, DocType.NOTE),
            title=stub.title,
            court="oireachtas",
            decision_date=stub.hint_date,
            language="en", source_language="en",
            landing_url=stub.raw_url,
            raw_bytes=blob, raw_ext=extension, text=text,
            extracted_via=ExtractedVia.SCRAPE,
            topic_tags=["oireachtas", "committee", family],
            extra={
                "jurisdiction": "ie",
                "committee_code": stub.hints.get("committee_code"),
                "house_no": stub.hints.get("house_no"),
                "document_family": family,
                "needs_ocr": needs_ocr,
                "extraction_engine": engine,
                # Everything a committee is sent is published here, and most of it is
                # procedure: minutes, covering letters, attendance. Storing that is fine;
                # putting it in front of a researcher is not. The gate keeps anything the
                # grammars find no statute or authority in out of retrieval — which is
                # exactly the split the sampling found, submissions citing Acts and the
                # minutes beside them citing nothing.
                "require_recognized_legal_citation": True,
            },
        )

    @staticmethod
    def _extension(stub: Stub, blob: bytes) -> str | None:
        declared = (stub.hints.get("extension") or "").lower()
        if declared in READABLE_EXTENSIONS:
            return declared
        if blob[:4] == b"%PDF":
            return "pdf"
        if blob[:2] == b"PK":
            return "docx"
        return None

    def _text(self, blob: bytes, extension: str) -> tuple[str, bool, str]:
        """Text, OCR'ing a scan that has no text layer. Committee evidence is often a
        letter that was signed, printed and scanned back in."""
        from ..extraction import extract_bytes, text_or_ocr

        if extension == "pdf":
            text, needs_ocr, _spans, engine = text_or_ocr(blob)
            if engine == "tesseract":
                log.info("%s: OCR'd a %d KB scan to %d characters",
                         self.source, len(blob) // 1024, len(text))
            return text.strip(), bool(needs_ocr), engine
        extracted = extract_bytes(blob, ext=extension)
        return (extracted.text or "").strip(), bool(extracted.needs_ocr), extracted.engine


def _int_or(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _since_date(since: str | None) -> date | None:
    try:
        return datetime.strptime(str(since or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
