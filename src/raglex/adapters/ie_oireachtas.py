"""Documents laid before the Houses of the Oireachtas — including committee reports.

Laying a document before the Dáil and the Seanad is how a great deal of Irish public
law is actually reported on: a committee's report, a regulator's annual report, the
accounts a statute obliges a body to produce, a post-enactment review, a treaty text.
Each one is laid *under* a named provision, which the catalogue records as its own
field — so this register does something a scrape of report PDFs cannot: it says which
section of which Act obliged the document to exist.

The register is the Oireachtas Library's public catalogue (``opac.oireachtas.ie``, a
PTFS Knowvation instance), 122,110 records from 1922 to today, of which 96,746 are the
Documents Laid collection. It is used here rather than the obvious route —
``www.oireachtas.ie/en/publications/`` — because that search **is behind an AWS WAF
captcha** ("Human Verification") and answers a plain client with HTTP 405 whatever
headers it is given, while the rest of that site is open. The catalogue is not walled,
holds the same documents with far better metadata, and reaches back seventy years
further.

Two things about the API are worth stating before reading the code, because both fail
*silently*:

- Every request needs a session. ``POST /aw-server/rest/register/user/publiclogin
  ?user=anonymous`` mints one (the site's own anonymous login, granting
  ``ROLE_PRODUCT_DOWNLOAD``); without the ``JSESSIONID`` and the ``X-XSRF-TOKEN`` header
  echoing the ``XSRF-TOKEN`` cookie, every call is a 401 with a plain-text body.
- ``SORTBY`` naming a field the catalogue does not declare returns **HTTP 200 with the
  results unsorted** (internal id order), not an error. A newest-first sweep that got
  the field name wrong would look like it worked and would harvest 1922 first, so the
  sort field here is one of the catalogue's own (``issued_date``, "Date Laid").

Defaults are the reports rather than the whole register. Statutory instruments are
40,643 of the laid documents and their text is already held from the Statute Book
(``ie-legislation``), so the sweep excludes that subcollection unless asked, and starts
at 1996 — thirty years of reports rather than seven decades of import quota orders.
Both are options; ``subcollections="*"``, ``include_statutory_instruments=true`` and
``since_year=1922`` restore the full archive.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Iterator
from urllib.parse import quote

from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType, ExtractedVia, Record, RelationshipType, ResolutionStatus, Stub,
    TypedRelation,
)

log = logging.getLogger("raglex.adapters.ie_oireachtas")

BASE = "https://opac.oireachtas.ie"
CSW = f"{BASE}/aw-server/awcsw"
LOGIN = f"{BASE}/aw-server/rest/register/user/publiclogin?user=anonymous"
#: The document bytes, for a record whose stored file is not reachable by a plain URL.
PROVIDER = f"{BASE}/aw-server/rest/service/awdocumentprovider/uuid"

#: Both public libraries: General Collections (everything laid, plus the Library's and
#: the Budget Office's own publications) and Historical Collections.
LIBRARIES = "library3_lib OR library7_lib"
PAGE_SIZE = 200
#: "Date Laid" — a field the catalogue declares, and the only date the laid collection
#: reliably carries. See the module docstring on what an undeclared sort field does.
SORT_NEWEST_FIRST = "issued_date:DESC"
#: For a collection with no Date Laid, sorting on it is meaningless; the catalogue's own
#: document id at least groups a collection in the order it was accessioned.
SORT_BY_ID = "lb_document_id:DESC"

DEFAULT_COLLECTION = "Documents Laid"
#: Collections whose records carry a Date Laid. The Library & Research Service and the
#: Parliamentary Budget Office publish rather than lay: 377 of the 378 L&RS records have
#: no ``issued_date`` at all, so filtering those by a date window silently returns
#: nothing. They are windowed by Year of Publication instead.
DATED_COLLECTIONS = frozenset({"Documents Laid"})

STATUTORY_INSTRUMENT = "Statutory Instrument"
#: The catalogue's own typo, on four records. ``NOT "Statutory Instrument"`` does not
#: exclude them, so the exclusion names both spellings.
STATUTORY_INSTRUMENT_TYPO = "Stautory Instrument"

#: Thirty years. Older laid material is overwhelmingly import-quota orders and
#: commencement orders; the reports are recent.
DEFAULT_FLOOR_YEAR = 1996

#: The subcollection each kind of laid document is tagged with. Roughly half of the
#: laid records carry no subcollection at all — those are the annual reports, accounts
#: and treaty texts, and they are the bulk of what this source is for, so an untagged
#: record is preparatory material rather than something to skip.
DOC_TYPES: dict[str, DocType] = {
    "Committee Report": DocType.PREPARATORY,
    "Committee Debate": DocType.PREPARATORY,
    "Ombudsman Report": DocType.PREPARATORY,
    "C&AG Special Report": DocType.PREPARATORY,
    "Post-enactment Report": DocType.PREPARATORY,
    "PBO Publication": DocType.PREPARATORY,
    "Bill Digest": DocType.PREPARATORY,
    "L&RS Note": DocType.PREPARATORY,
    "EU Scrutiny Six-Month Report": DocType.PREPARATORY,
    "Standing Orders": DocType.PREPARATORY,
    # Scrutiny notes and the weekly EU report are working notes, not argued reports.
    "EU Scrutiny Information Note": DocType.NOTE,
    "EU Weekly Report": DocType.NOTE,
    "Data Sharing Agreement": DocType.NOTE,
    STATUTORY_INSTRUMENT: DocType.LEGISLATION,
    STATUTORY_INSTRUMENT_TYPO: DocType.LEGISLATION,
}

#: The user asked for these two and the catalogue is, as sampled, entirely PDF — but the
#: gate is on the file the record actually points at, not on that assumption.
READABLE_EXTENSIONS = ("pdf", "docx")

#: A laid document is a report or a set of accounts; the largest sampled is 29 MB. A
#: file an order of magnitude past that is a plate scan of a bound volume, and reading
#: it costs more than it returns. Overridable per run.
DEFAULT_MAX_KB = 120_000


# --- the catalogue's own encodings -------------------------------------------------

_JAVA_DATE = re.compile(
    r"^\w{3}\s+(?P<mon>\w{3})\s+(?P<day>\d{1,2})\s+[\d:]+\s+\S+\s+(?P<year>\d{4})$")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def parse_catalogue_date(value: str | None) -> date | None:
    """``"Fri Jul 31 10:26:21 GMT 2026"`` → ``date(2026, 7, 31)``.

    The catalogue serialises dates as Java's ``Date.toString()``. ``%Z`` will not parse
    it portably (the zone is whatever the server's locale printed — GMT or IST), so the
    parts that matter are read directly."""
    match = _JAVA_DATE.match((value or "").strip())
    if not match:
        return None
    month = _MONTHS.get(match.group("mon"))
    if not month:
        return None
    try:
        return date(int(match.group("year")), month, int(match.group("day")))
    except ValueError:
        return None


def parse_size_kb(value: str | None) -> int | None:
    """``"1,673 KB"`` → ``1673``. The catalogue reports every file in KB."""
    match = re.match(r"^\s*([\d,]+)\s*KB\s*$", str(value or ""), re.I)
    return int(match.group(1).replace(",", "")) if match else None


def file_extension(record: dict) -> str | None:
    """The extension of the file a record points at, from either path it publishes."""
    for key in ("document_path", "legacy_virtual_path"):
        path = str(record.get(key) or "").split("?", 1)[0]
        if "." in path.rsplit("/", 1)[-1]:
            return path.rsplit(".", 1)[-1].lower()
    return None


def sniff_extension(blob: bytes) -> str | None:
    """The format from the bytes, for a record whose stored path carries no extension.

    Only the two formats this source reads: a PDF's ``%PDF`` header and a DOCX's zip
    magic. Anything else is not a document to read."""
    if blob[:4] == b"%PDF":
        return "pdf"
    if blob[:2] == b"PK":
        return "docx"
    return None


_LAID_UNDER = re.compile(r"^(?P<act>.+?)\s*--\s*(?P<anchor>.+)$")
_HAS_YEAR = re.compile(r"\b(1[6-9]\d{2}|20\d{2})\b")


def laid_under(value: str | None) -> tuple[str, str | None] | None:
    """``"Agriculture act 1931 -- Section 46(1)"`` → ``("Agriculture Act 1931",
    "Section 46(1)")``.

    "Legislation Laid Under" is the provision that obliged this document to be laid, and
    it is the one citation a laid document is guaranteed to have — most annual reports
    name their enabling section nowhere in the PDF. Two things it is not: ``"N/A"``
    (about a fifth of the register), and a standing order ("SO 109 Dáil and SO 86
    Seanad"), which is Oireachtas procedure rather than legislation. Requiring a year
    keeps both out, since every Act and S.I. title carries one.

    The catalogue writes titles in sentence case ("Agriculture act 1931"), which no
    citation grammar recognises; the word is capitalised back so the raw string is one
    the resolver can match against the Statute Book."""
    text = " ".join(str(value or "").split())
    if not text or text.upper() == "N/A":
        return None
    match = _LAID_UNDER.match(text)
    act, anchor = (match.group("act"), match.group("anchor")) if match else (text, None)
    if not _HAS_YEAR.search(act):
        return None
    act = re.sub(r"\b(act|acts|order|orders|regulations|rules|scheme)\b",
                 lambda m: m.group(1).capitalize(), act)
    return act.strip(), (anchor.strip() if anchor else None)


def awql(*, collections: tuple[str, ...], query: str = "*",
         subcollections: tuple[str, ...] = (), exclude_subcollections: tuple[str, ...] = (),
         window: str | None = None, years: tuple[int, ...] = ()) -> str:
    """One AWQL_FORM constraint string.

    The forms are the site's own: a bracketed value list per field, ``OR`` inside the
    brackets, and ``NOT`` for exclusion. ``-"value"`` looks like exclusion and is not —
    it matches fuzzily, and asking for ``-"Statutory Instrument"`` returns the four
    records whose subcollection is misspelt ``Stautory Instrument``."""
    parts = [f"qs=[{query or '*'}]", "queryType=[16]", f"library=[{LIBRARIES}]"]
    if collections:
        parts.append(f"browse2=[{_or_values(collections)}]")
    if subcollections:
        parts.append(f"usertext17=[{_or_values(subcollections)}]")
    elif exclude_subcollections:
        parts.append(f"usertext17=[NOT ({_or_values(exclude_subcollections)})]")
    if window:
        parts.append(f"issued_date=[{window}]")
    if years:
        parts.append(f"browse3=[{_or_values(tuple(str(y) for y in years))}]")
    return ",".join(parts)


def _or_values(values: tuple[str, ...]) -> str:
    return " OR ".join(f'"{v}"' for v in values)


def date_window(start: date, end: date) -> str:
    """``01/01/1996-12/31/2026`` — the catalogue's date-range literal (US order)."""
    return f"{start.strftime('%m/%d/%Y')}-{end.strftime('%m/%d/%Y')}"


def month_windows(floor: date, today: date) -> list[tuple[date, date]]:
    """Calendar months from ``today`` back to ``floor``, newest first.

    Discovery is windowed because the result set a search returns carries no date: the
    summary element set is fixed (``ELEMENTSETNAME=full`` on a search is accepted and
    ignored) and has no Date Laid in it. The window a record was found in is therefore
    the only date discovery knows, and one month is a tight enough cursor that an
    incremental run re-reads at most a month of already-held stubs."""
    windows: list[tuple[date, date]] = []
    start = date(today.year, today.month, 1)
    while start >= date(floor.year, floor.month, 1):
        end = _end_of_month(start)
        windows.append((max(start, floor), min(end, today)))
        start = (start - timedelta(days=1)).replace(day=1)
    return windows


def _end_of_month(start: date) -> date:
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def stable_id(record: dict) -> str | None:
    """``ie/oireachtas/opac/215643`` — the catalogue's own document id.

    A laid document is *cited* by its DL number ("DL211160"), which would be the better
    key, but the search result does not carry it: only the per-record detail call does,
    and identity has to be minted in discovery or every incremental run re-fetches the
    whole register to learn what it already holds. The DL number is registered as a
    citation alias when the document is fetched, so a reference to it resolves here."""
    doc_id = str(record.get("lb_document_id") or record.get("id") or "").strip()
    return f"ie/oireachtas/opac/{doc_id}" if doc_id else None


def record_url(record: dict) -> str | None:
    """The file itself. ``legacy_virtual_path`` is a plain static URL that needs no
    session, so it is preferred; the record's uuid feeds the authenticated document
    service for anything published without one."""
    path = str(record.get("legacy_virtual_path") or "").strip()
    if path.startswith("http"):
        return path.replace("http://", "https://", 1)
    uuid = str(record.get("uuid") or "").strip()
    return f"{PROVIDER}/{uuid}" if uuid else None


def landing_url(record: dict) -> str:
    """The catalogue page a reader can open — the OPAC's own deep link to one record."""
    doc_id = str(record.get("lb_document_id") or record.get("id") or "").strip()
    return f"{BASE}/s?v=L&a=c&criteria={quote(f'lb_document_id={doc_id}')}"


def search_results(payload: dict) -> tuple[list[dict], int]:
    """``(records, total)`` from a CSW GetRecords response."""
    results = ((payload or {}).get("csw:GetRecordsResponse") or {}).get(
        "csw:SearchResults") or {}
    records = results.get("iStoreRecord") or []
    if isinstance(records, dict):  # a single hit is not wrapped in a list
        records = [records]
    return [r for r in records if isinstance(r, dict)], int(
        results.get("numberOfRecordsMatched") or 0)


def detail_record(payload: dict) -> dict:
    record = ((payload or {}).get("csw:GetRecordByIdResponse") or {}).get(
        "iStoreRecord") or {}
    return record if isinstance(record, dict) else {}


def split_list(value: str | None) -> list[str]:
    """The catalogue joins repeated values with ``;`` (subjects, creators)."""
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def doc_type_for(subcollection: str | None) -> DocType:
    return DOC_TYPES.get((subcollection or "").strip(), DocType.PREPARATORY)


# --- the adapter -------------------------------------------------------------------


class OireachtasLaidAdapter(BaseAdapter):
    source = "ie-oireachtas"
    min_interval = 0.5

    def __init__(self, *, client: RateLimitedClient | None = None,
                 query: str | None = None,
                 collections: str | None = None,
                 subcollections: str | None = None,
                 include_statutory_instruments=None,
                 since_year=None,
                 max_kb=None,
                 start_offset: int = 0) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)
        self._token: str | None = None
        self.query = " ".join(str(query).split()) if query else "*"
        self.collections = tuple(
            part.strip() for part in str(collections).split(",") if part.strip()
        ) if collections else (DEFAULT_COLLECTION,)
        # "*" is how a user asks for every subcollection *including* the instruments —
        # an explicit list is taken literally, so it can also be used to sweep only
        # committee reports.
        wanted = str(subcollections or "").strip()
        self.subcollections: tuple[str, ...] = () if wanted in ("", "*") else tuple(
            part.strip() for part in wanted.split(",") if part.strip())
        self.include_statutory_instruments = option_flag(
            include_statutory_instruments, wanted == "*")
        self.since_year = option_int(since_year, DEFAULT_FLOOR_YEAR)
        self.max_kb = option_int(max_kb, DEFAULT_MAX_KB)
        # Emitting resume_offset obliges us to accept it back (see be_gba_decisions).
        self.start_offset = max(0, int(start_offset or 0))

    # ---- session ------------------------------------------------------------------

    def _login(self) -> None:
        """Take the catalogue's anonymous session.

        The token is echoed from the cookie into ``X-XSRF-TOKEN`` on every later call;
        the cookie alone is a 401, and so is the header alone — the session cookie rides
        on the shared httpx client, so one login covers the whole run."""
        response = self._client.request("POST", LOGIN, json={},
                                        headers={"Content-Type": "application/json"})
        self._token = response.cookies.get("XSRF-TOKEN") or _client_token(self._client)

    def _call(self, params: dict) -> dict:
        """One CSW call, re-authenticating once if the session has lapsed.

        A dropped session is an ordinary 401 mid-run — the catalogue expires them — and
        it must not read as "this document is gone"."""
        for attempt in (0, 1):
            if self._token is None:
                self._login()
            try:
                return self._client.get(
                    CSW, params=params,
                    headers={"Accept": "application/json; charset=UTF-8",
                             "X-XSRF-TOKEN": self._token or ""},
                ).json()
            except FetchError as exc:
                if attempt or "401" not in str(exc):
                    raise
                self._token = None
        return {}

    def _search(self, constraint: str, *, start: int, sort: str) -> tuple[list[dict], int]:
        return search_results(self._call({
            "CONSTRAINT": _b64(constraint),
            "typenames": "aw:iStoreRecord",
            "OUTPUTFORMAT": "application/json",
            "SERVICE": "CSW",
            "REQUEST": "GetRecords",
            "CONSTRAINTLANGUAGE": "AWQL_FORM",
            "VERSION": "2.0.2",
            "ELEMENTSETNAME": "summary",
            "RESULTTYPE": "results",
            "STARTPOSITION": start,
            "MAXRECORDS": PAGE_SIZE,
            "SORTBY": sort,
            "INVOKER": "SYSTEM",
        }))

    def _detail(self, uuid: str) -> dict:
        """The full catalogue record: date laid, DL number, laid-under provision,
        subjects, statutory period. None of it is in a search result — the search's
        element set is fixed, and asking for ``full`` there changes nothing."""
        return detail_record(self._call({
            "SERVICE": "CSW", "REQUEST": "GetRecordById", "VERSION": "2.0.2",
            "OUTPUTFORMAT": "application/json", "ELEMENTSETNAME": "full",
            "typenames": "aw:iStoreRecord", "ID": uuid, "INVOKER": "SYSTEM",
        }))

    # ---- discovery ----------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None,
                 today: date | None = None) -> Iterator[Stub]:
        """Newest month first, back to the cursor or the configured floor.

        Each window is filtered server-side, so an incremental run asks for one month
        and stops. The cursor a stub carries is the START of its window rather than its
        own date laid, which discovery does not know: it is the honest cursor for a
        month-windowed walk, and it costs one re-read of the current month per run."""
        today = today or date.today()
        floor = _floor_date(since, self.since_year)
        dated = [c for c in self.collections if c in DATED_COLLECTIONS]
        undated = [c for c in self.collections if c not in DATED_COLLECTIONS]
        skipped, pages, position = self.start_offset, 0, self.start_offset
        total = self._feed_total(floor, today, dated, undated)

        for group in self._plans(floor, today, dated, undated):
            constraint, cursor, sort = group
            start = 1
            while True:
                if max_pages is not None and pages >= max_pages:
                    return
                try:
                    records, _matched = self._search(constraint, start=start, sort=sort)
                except (FetchError, ValueError) as exc:
                    log.warning("%s: search failed at %s+%d: %s",
                                self.source, cursor, start, exc)
                    break
                if not records:
                    # A month in which nothing was laid costs one cheap request and does
                    # NOT spend the page budget: the windows are a finite, known list, so
                    # the walk stays bounded either way, and counting empty months would
                    # let a quiet summer exhaust ``max_pages`` before a single document.
                    break
                pages += 1
                for record in records:
                    if skipped:
                        skipped -= 1
                        continue
                    stub = self._stub(record, cursor)
                    if stub is None:
                        continue
                    stub.hints["resume_offset"] = position
                    if total:
                        stub.hints["feed_total"] = total
                    position += 1
                    yield stub
                if len(records) < PAGE_SIZE:
                    break
                start += len(records)

    def _plans(self, floor: date, today: date, dated: list[str],
               undated: list[str]) -> list[tuple[str, str, str]]:
        """``(constraint, cursor, sort)`` per query, newest material first."""
        plans: list[tuple[str, str, str]] = []
        if dated:
            for start, end in month_windows(floor, today):
                plans.append((
                    awql(collections=tuple(dated), query=self.query,
                         subcollections=self.subcollections,
                         exclude_subcollections=self._excluded(),
                         window=date_window(start, end)),
                    start.isoformat(), SORT_NEWEST_FIRST))
        if undated:
            # No Date Laid to window by, and only a few hundred records: one walk of
            # the whole collection, filtered by Year of Publication.
            years = tuple(range(today.year, floor.year - 1, -1))
            plans.append((
                awql(collections=tuple(undated), query=self.query,
                     subcollections=self.subcollections,
                     exclude_subcollections=self._excluded(), years=years),
                f"{floor.year}-01-01", SORT_BY_ID))
        return plans

    def _excluded(self) -> tuple[str, ...]:
        if self.subcollections or self.include_statutory_instruments:
            return ()
        return (STATUTORY_INSTRUMENT, STATUTORY_INSTRUMENT_TYPO)

    def _feed_total(self, floor: date, today: date, dated: list[str],
                    undated: list[str]) -> int:
        """The register's own count for the whole sweep, so the job draws a real bar.

        One extra request, and it is authoritative — the catalogue reports
        ``numberOfRecordsMatched`` for the unpaged query."""
        total = 0
        try:
            if dated:
                _rows, matched = self._search(
                    awql(collections=tuple(dated), query=self.query,
                         subcollections=self.subcollections,
                         exclude_subcollections=self._excluded(),
                         window=date_window(floor, today)),
                    start=1, sort=SORT_NEWEST_FIRST)
                total += matched
            if undated:
                _rows, matched = self._search(
                    awql(collections=tuple(undated), query=self.query,
                         subcollections=self.subcollections,
                         exclude_subcollections=self._excluded(),
                         years=tuple(range(today.year, floor.year - 1, -1))),
                    start=1, sort=SORT_BY_ID)
                total += matched
        except (FetchError, ValueError):
            return 0
        return total

    def _stub(self, record: dict, cursor: str) -> Stub | None:
        key = stable_id(record)
        url = record_url(record)
        if not key or not url:
            return None
        return Stub(
            stable_id=key,
            landing_url=landing_url(record),
            raw_url=url,
            title=" ".join(str(record.get("title") or "").split()) or None,
            court="oireachtas",
            hints={
                "uuid": record.get("uuid"),
                "document_id": record.get("lb_document_id") or record.get("id"),
                "size_kb": parse_size_kb(record.get("product_size")),
                "extension": file_extension(record),
                "watermark": cursor,
            },
        )

    # ---- fetch --------------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        extension = (stub.hints.get("extension") or "").lower()
        if extension and extension not in READABLE_EXTENSIONS:
            # A laid document published as a spreadsheet is a table of numbers with no
            # text engine behind it; byte-decoding it would store zip noise as the text.
            log.info("%s: %s is a .%s, not a document to read",
                     self.source, stub.stable_id, extension)
            return None
        size_kb = stub.hints.get("size_kb")
        if size_kb and self.max_kb and size_kb > self.max_kb:
            log.warning("%s: %s is %d KB (limit %d) — skipped",
                        self.source, stub.stable_id, size_kb, self.max_kb)
            return None

        uuid = str(stub.hints.get("uuid") or "")
        try:
            detail = self._detail(uuid) if uuid else {}
        except (FetchError, ValueError):
            detail = {}
        blob = self._document(stub, uuid)
        if not blob:
            return None
        extension = extension or sniff_extension(blob)
        if extension is None:
            # A record whose catalogue path had no extension and whose bytes are neither
            # format. Guessing "docx" here would hand a JPEG to a zip reader and store
            # the empty result as the document's text.
            log.info("%s: %s is neither a PDF nor a DOCX", self.source, stub.stable_id)
            return None
        text, needs_ocr, engine = self._text(blob, extension)
        if not text and not needs_ocr:
            return None
        return self._record(stub, detail, blob, extension, text, needs_ocr, engine)

    def _document(self, stub: Stub, uuid: str) -> bytes | None:
        """The file, by its static URL, falling back to the authenticated service.

        Both routes serve the same bytes; the static one is preferred because it costs
        no session and survives an expired one."""
        for url in (stub.raw_url, f"{PROVIDER}/{uuid}" if uuid else None):
            if not url:
                continue
            try:
                if url.startswith(PROVIDER):
                    if self._token is None:
                        self._login()
                    response = self._client.get(
                        url, headers={"X-XSRF-TOKEN": self._token or ""})
                else:
                    response = self._client.get(url)
            except FetchError as exc:
                log.info("%s: %s could not be downloaded from %s: %s",
                         self.source, stub.stable_id, url, exc)
                continue
            if response.content:
                return response.content
        return None

    def _text(self, blob: bytes, extension: str) -> tuple[str, bool, str]:
        """Text, and whether it is a scan nothing here could read.

        The laid archive is digitised paper back to 1922. Much of it already carries an
        OCR layer from the digitisation and parses born-digital; the rest is a
        photograph of a document and escalates. ``needs_ocr`` stays True only when the
        scan could not be read at all — a successful OCR pass is not a review item.

        An escalation is logged because it is the one thing here that takes minutes: at
        roughly ten seconds a page a scanned annual report is a long, silent worker, and
        without this line a job sitting quietly on one document has no visible cause."""
        from ..extraction import extract_bytes, text_or_ocr

        if extension == "pdf":
            text, needs_ocr, _spans, engine = text_or_ocr(blob)
            if engine == "tesseract":
                log.info("%s: OCR'd a %d KB scan to %d characters",
                         self.source, len(blob) // 1024, len(text))
            elif needs_ocr:
                log.warning("%s: a %d KB scan could not be read — no OCR stack here",
                            self.source, len(blob) // 1024)
            return text.strip(), bool(needs_ocr), engine
        extracted = extract_bytes(blob, ext=extension)
        return (extracted.text or "").strip(), bool(extracted.needs_ocr), extracted.engine

    def _record(self, stub: Stub, detail: dict, blob: bytes, extension: str,
                text: str, needs_ocr: bool, engine: str) -> Record:
        subcollection = str(detail.get("usertext17") or "").strip() or None
        laid = parse_catalogue_date(detail.get("issued_date"))
        dl_number = str(detail.get("field20") or "").strip() or None
        provision = laid_under(detail.get("field17"))
        title = " ".join(str(detail.get("title") or stub.title or "").split()) or None
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=doc_type_for(subcollection),
            title=title,
            court="oireachtas",
            decision_date=laid,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=blob, raw_ext=extension, text=text,
            relations=self._relations(provision),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["oireachtas", "documents-laid",
                        *(["committee"] if subcollection == "Committee Report" else [])],
            extra={
                "jurisdiction": "ie",
                # The DL number is how the Order Papers cite a laid document; registering
                # it as an alias is what lets "DL211160" resolve to this record, given
                # the stable id has to be minted before the number is known.
                "aliases": [dl_number] if dl_number else [],
                "dl_number": dl_number,
                "collection": str(detail.get("browse2") or "").strip() or None,
                "subcollection": subcollection,
                "laid_before": str(detail.get("field6") or "").strip() or None,
                "laid_under": " ".join(str(detail.get("field17") or "").split()) or None,
                "date_laid": laid.isoformat() if laid else None,
                "originating_authority": str(detail.get("contributor") or "").strip() or None,
                "creators": split_list(detail.get("creator")),
                "subjects": split_list(detail.get("subject")),
                "order_paper_dail": str(detail.get("field18") or "").strip() or None,
                "order_paper_seanad": str(detail.get("field19") or "").strip() or None,
                # The annulment apparatus: the period either House has to annul the
                # instrument, when it expires, and whether it was annulled.
                "statutory_period": str(detail.get("field24") or "").strip() or None,
                "motion_of_approval": str(detail.get("field15") or "").strip() or None,
                "annulled_date": str(detail.get("field39") or "").strip() or None,
                "prn": str(detail.get("field21") or "").strip() or None,
                "notes": " ".join(str(detail.get("notes") or "").split()) or None,
                "opac_uuid": stub.hints.get("uuid"),
                "opac_document_id": stub.hints.get("document_id"),
                "needs_ocr": needs_ocr,
                "extraction_engine": engine,
            },
        )

    @staticmethod
    def _relations(provision: tuple[str, str | None] | None) -> list[TypedRelation]:
        """The enabling provision, as an edge.

        "Legislation Laid Under" is structured metadata, not a string found in the text,
        so it is recorded as such and left PENDING for the resolver to match against the
        Statute Book — the Act is very often named nowhere in the document itself."""
        if not provision:
            return []
        act, anchor = provision
        return [TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string=f"{anchor} of the {act}" if anchor else act,
            dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        )]


def _floor_date(since: str | None, since_year: int) -> date:
    """The oldest month a sweep will ask for: the cursor, or the configured year."""
    floor = date(max(1922, int(since_year or DEFAULT_FLOOR_YEAR)), 1, 1)
    head = str(since or "")[:10]
    try:
        cursor = datetime.strptime(head, "%Y-%m-%d").date()
    except ValueError:
        return floor
    return max(floor, cursor)


def _client_token(client: RateLimitedClient) -> str | None:
    """The XSRF cookie off the client's jar, for a login whose response set it earlier
    in a redirect chain rather than on the final response."""
    jar = getattr(getattr(client, "_client", None), "cookies", None)
    return jar.get("XSRF-TOKEN") if jar is not None else None


def _b64(value: str) -> str:
    import base64

    return base64.b64encode(value.encode("utf-8")).decode("ascii")
