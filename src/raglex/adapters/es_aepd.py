"""AEPD resoluciones — Spain's data-protection enforcement register, entire.

The Agencia Española de Protección de Datos publishes every resolution it signs: some
46,900 of them, from 2007 to today, ten to a Drupal listing page. That makes it one of
the largest single-authority enforcement corpora in Europe and by some distance the
largest in this repository outside the court registers — the Belgian, Irish and Dutch
authorities publish in the low thousands.

## What a resolution is

The title is the file number and encodes the procedure, which is the fact a reader most
wants to filter on:

    PS-00355-2025          procedimiento sancionador — a fining decision
    TD-00148-2016          tutela de derechos — a data-subject rights complaint upheld
    AI-00023-2025          apercibimiento / actuaciones de investigación
    PD-00110-2026          procedimiento de derechos
    E-01103-2005           expediente (the oldest series)
    REPOSICION-PS-00503-2024   a reposición appeal against one of the above

``REPOSICION-`` prefixes the number of the decision under appeal, so the pair is
discoverable from the id alone. The listing also carries the *fecha de firma*, which is
the decision date, and a teaser of the text; the operative document is the PDF at
``/documento/{number}.pdf``.

## Two surfaces, and only one of them can backfill

* The **listing** pages the whole archive, newest first, and is the backfill path. It
  exposes a durable ``?page=N`` cursor, so an interrupted run resumes — this adapter
  reports ``resume_offset`` and therefore accepts ``start_offset`` back (AGENTS.md §1).
* The **RSS feed** (``/informes-y-resoluciones/resoluciones/feed.xml``) holds the newest
  hundred items and is the keep-current path. It carries **no ``pubDate``** — every
  item's element is empty — so it cannot be filtered by date and is not ordered by one
  either. It is used as an id set: everything in it is offered, and the pipeline drops
  what it already holds. That is why the watch also re-reads the first few listing
  pages, which *do* carry dates: a hundred items is under three weeks of output at the
  current rate, and a monthly watch that trusted only the feed would miss whatever fell
  off the end of it.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_flag, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub
from ..extraction.ocr import text_or_ocr
from ._governing_instrument import GDPR, default_instrument

log = logging.getLogger(__name__)

BASE = "https://www.aepd.es"
LISTING = f"{BASE}/informes-y-resoluciones/resoluciones"
FEED = f"{LISTING}/feed.xml"
PAGE_SIZE = 10
#: How many listing pages a keep-current run re-reads alongside the feed. Ten pages is a
#: hundred resolutions — the feed's whole depth — and costs ten requests a month.
WATCH_PAGES = 10

#: The procedure each file-number prefix names. Kept as a label rather than folded into
#: a topic tag: the difference between a sanción and a tutela de derechos is the
#: difference between a fine and a rights order, and a reader filters on it.
PROCEDURES: dict[str, str] = {
    "PS": "Procedimiento sancionador",
    "PD": "Procedimiento de derechos",
    "TD": "Tutela de derechos",
    "AI": "Actuaciones de investigación",
    "AP": "Apercibimiento",
    "E": "Expediente",
    "EXP": "Expediente",
    "RR": "Recurso de reposición",
    "SA": "Sanción a administraciones públicas",
    "AAPP": "Administraciones públicas",
}
#: "REPOSICION-PS-00503-2024" — an appeal, whose number is the decision it challenges.
_REPOSICION = re.compile(r"^\s*(?:RE?POSICION|REPOSICIÓN)[-\s]+(?P<base>.+)$",
                         re.IGNORECASE)
_FILE_NUMBER = re.compile(r"^(?P<prefix>[A-Z]{1,4})[-/](?P<serial>\d{1,6})[-/](?P<year>\d{4})$")


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _iso(value: str | None) -> date | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def file_number(title: str) -> str:
    """The resolution's number, normalised: upper case, hyphen separated."""
    return re.sub(r"[\s/]+", "-", _clean(title)).upper()


def stable_id(title: str) -> str:
    return f"es/aepd/resolucion/{file_number(title).casefold()}"


def appealed_decision(title: str) -> str | None:
    """The id of the decision a *reposición* challenges, or ``None``.

    An appeal and the decision it is against share a number, so the pair can be linked
    from the id alone rather than by reading either document. Without this the register
    holds both and says nothing about the relation.
    """
    match = _REPOSICION.match(_clean(title))
    return stable_id(match.group("base")) if match else None


def procedure(title: str) -> tuple[str | None, str | None]:
    """``("PS", "Procedimiento sancionador")`` from a file number, appeal or not."""
    text = file_number(title)
    match = _REPOSICION.match(text)
    if match:
        text = file_number(match.group("base"))
    parsed = _FILE_NUMBER.match(text)
    if not parsed:
        return None, None
    prefix = parsed.group("prefix").upper()
    return prefix, PROCEDURES.get(prefix)


def listing_items(html: bytes | str) -> list[dict]:
    """One dict per resolution teaser, in page order.

    Drupal renders each resolution as one ``<article class="node--type-resolucion-
    reclamacion …">``; the promoted rows on deeper pages carry an extra ``node--promoted``
    class, so the type class is matched rather than the whole attribute — an exact-string
    match silently returned nothing from page 2 onwards.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for position, article in enumerate(
            soup.select("article.node--type-resolucion-reclamacion")):
        heading = article.select_one(".field--name-title h2") or article.find("h2")
        link = article.select_one(".field--name-fichero a[href]") or article.select_one(
            'a[href*="/documento/"]')
        if heading is None or link is None:
            continue
        stamp = article.select_one(".field--name-fecha-firma time[datetime]")
        summary = article.select_one(".field--name-body")
        out.append({
            "title": _clean(heading.get_text(" ", strip=True)),
            "url": urljoin(BASE, str(link.get("href") or "")),
            "date": _iso(str(stamp["datetime"]) if stamp is not None else None),
            "summary": _clean(summary.get_text(" ", strip=True)) if summary else None,
            "position": position,
        })
    return out


def last_page(html: bytes | str) -> int:
    """The pager's own last-page number — the only place the archive states its size."""
    soup = BeautifulSoup(html, "html.parser")
    pages = [0]
    for link in soup.select("nav[aria-label] a[href]"):
        match = re.search(r"[?&]page=(\d+)", str(link.get("href") or ""))
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def feed_items(xml: bytes | str) -> list[dict]:
    """The newest ~100 resolutions.

    ``<pubDate/>`` is empty in every item the AEPD publishes, so no date is read from
    here and none is invented: the listing supplies dates, and a feed-discovered stub
    carries none rather than today's.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    out: list[dict] = []
    for item in ET.fromstring(xml).iter("item"):
        title = _clean(item.findtext("title"))
        url = _clean(item.findtext("link"))
        if not title or not url:
            continue
        out.append({"title": title, "url": url, "date": None,
                    "summary": _clean(item.findtext("description")) or None})
    return out


class AEPDResolutionsAdapter(BaseAdapter):
    source = "es-aepd-resoluciones"
    min_interval = 1.0

    def __init__(
        self, *, start_offset: int | str | None = None,
        max_ocr_pages: int | str | None = None, ocr: bool | str | None = None,
        watch_pages: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        # Reported as ``resume_offset`` on every stub, so it MUST be accepted back:
        # ``jobs`` passes it to this constructor when it resumes an interrupted
        # backfill, and an adapter that cannot take it raises TypeError and files the
        # retry as done. ``resume_floor`` backs off one page deliberately.
        self.start_offset = resume_floor(option_int(start_offset, 0), PAGE_SIZE)
        self.ocr = option_flag(ocr, True)
        self.max_ocr_pages = max(0, option_int(max_ocr_pages, 120))
        self.watch_pages = max(1, option_int(watch_pages, WATCH_PAGES))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    # -- discovery ----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if since:
            yield from self._watch(since)
            return
        yield from self._walk(max_pages)

    def _watch(self, since: str) -> Iterator[Stub]:
        """The feed, then the newest listing pages.

        Both, not either. The feed has no dates and a fixed depth of a hundred; the
        listing has dates and no ceiling. Reading only the feed loses anything published
        faster than the watch runs, and reading only the listing loses an item the AEPD
        back-dates into the middle of the archive — which it does, because the sort key
        is the signature date and publication lags it by weeks.
        """
        seen: set[str] = set()
        try:
            rows = feed_items(self._client.get(FEED).content)
        except (FetchError, ET.ParseError):
            log.warning("%s: RSS unavailable; falling back to the listing", self.source)
            rows = []
        for row in rows:
            key = stable_id(row["title"])
            seen.add(key)
            yield self._stub(row, key=key, discovered_via="rss", feed_total=len(rows))
        cutoff = _iso(str(since)[:10])
        for page in range(0, self.watch_pages):
            try:
                body = self._client.get(LISTING, params={"page": page}).content
            except FetchError:
                return
            items = listing_items(body)
            if not items:
                return
            for row in items:
                key = stable_id(row["title"])
                if key in seen:
                    continue
                seen.add(key)
                if cutoff and row["date"] and row["date"] < cutoff:
                    continue
                yield self._stub(row, key=key, discovered_via="listing")
            # Stop when a WHOLE page is older than the cursor, never on the first old
            # row: the listing is ordered by signature date and the AEPD signs in
            # batches, so an older row partway down a page is ordinary. A resolution
            # signed months ago and published today sits deep in the list and is not
            # reachable this way at all — that is what the feed pass above is for.
            if cutoff and items and all(
                    row["date"] and row["date"] < cutoff for row in items):
                return

    def _walk(self, max_pages: int | None) -> Iterator[Stub]:
        try:
            first = self._client.get(LISTING).content
        except FetchError:
            return
        end = last_page(first)
        total = (end + 1) * PAGE_SIZE if end else None
        first_page = self.start_offset // PAGE_SIZE
        if max_pages is not None:
            end = min(end, first_page + max(0, max_pages) - 1)
        for page in range(first_page, end + 1):
            body = first if page == 0 else self._client.get(
                LISTING, params={"page": page}).content
            items = listing_items(body)
            if not items:
                # A page inside a 4,690-page archive that parses to nothing is a broken
                # parser or a broken response, not the end of the register — the pager
                # already told us where that is. Raising keeps a truncated backfill from
                # recording itself as complete.
                raise ValueError(
                    f"{self.source}: listing page {page} of {end} has no resolutions")
            for row in items:
                yield self._stub(
                    row, key=stable_id(row["title"]), discovered_via="listing",
                    feed_total=total,
                    resume_offset=page * PAGE_SIZE + int(row["position"]))

    def _stub(self, row: dict, *, key: str, discovered_via: str,
              feed_total: int | None = None,
              resume_offset: int | None = None) -> Stub:
        hints: dict = {
            "summary": row.get("summary"), "discovered_via": discovered_via,
            "watermark": row["date"].isoformat() if row.get("date") else None,
        }
        if feed_total:
            hints["feed_total"] = int(feed_total)
        if resume_offset is not None:
            hints["resume_offset"] = int(resume_offset)
        return Stub(
            stable_id=key, landing_url=row["url"], raw_url=row["url"],
            title=row["title"], court="dpa-es", hint_date=row.get("date"), hints=hints)

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        try:
            pdf = self._client.get(stub.raw_url).content
        except FetchError:
            return None
        if not pdf.startswith(b"%PDF"):
            log.warning("%s: not a PDF: %s", self.source, stub.raw_url)
            return None
        text, needs_ocr, spans, engine = text_or_ocr(
            pdf, max_pages=self.max_ocr_pages if self.ocr else 0)
        if len((text or "").strip()) < 200:
            needs_ocr = True
        segments = [Segment(label=f"p. {number}", char_start=start, char_end=end,
                            kind="page") for number, start, end in spans]
        title = stub.title or file_number(stub.stable_id.rsplit("/", 1)[-1])
        code, label = procedure(title)
        appealed = appealed_decision(title)
        return Record(
            source=self.source, stable_id=stub.stable_id,
            # A supervisory authority's determination is administrative, not case law
            # and not guidance — ``docs/adapter-authoring.md``.
            doc_type=DocType.DECISION, title=title, court="dpa-es",
            decision_date=stub.hint_date, language="es", source_language="es",
            landing_url=stub.landing_url, raw_bytes=pdf, raw_ext="pdf",
            text=text or None, segments=segments, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=["data-protection", "spain", "aepd", "enforcement",
                        *([label.casefold()] if label else [])],
            extra={
                "jurisdiction": "es",
                "issuer": "Agencia Española de Protección de Datos",
                "file_number": file_number(title),
                "procedure_code": code, "procedure": label,
                # Set only on a reposición: the decision it challenges, by the id that
                # decision is stored under. The relation is in the number and nowhere
                # else, so not minting it here loses it.
                "appeal_against": appealed,
                "is_appeal": bool(appealed),
                "summary": stub.hints.get("summary"),
                "pdf_url": stub.raw_url,
                "discovered_via": stub.hints.get("discovered_via"),
                "format": engine, "needs_ocr": needs_ocr or None,
                # A resolution recites its legal basis once in the *fundamentos* and
                # then argues in bare articles for pages. The GDPR is the regime unless
                # the file's own title names another instrument.
                "citation_default_instrument": default_instrument(title, GDPR),
                # An anonymised resolution is mostly A.A.A. and B.B.B.; several of the
                # shortest cite nothing formally at all. Gating on a recognised citation
                # would drop the archive's tail rather than its noise.
                "require_recognized_legal_citation": False,
            },
        )
