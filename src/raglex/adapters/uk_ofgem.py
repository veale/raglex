"""Ofgem's own publication register — 24,000 documents the GOV.UK feed never had.

RagLex already holds ``uk-ofgem``: Ofgem's pages on GOV.UK, which are a couple of
hundred corporate items. The regulator's actual output — every decision, consultation,
licence modification, code modification, guidance note and enforcement case since 1998
— lives on ``ofgem.gov.uk`` and is not on GOV.UK at all. This adapter is that register:

    https://www.ofgem.gov.uk/publications        (24,059 items at the time of writing)

**The listing is a real API, not a page to scrape.** The publications screen is a Vue
app talking to Drupal at ``/api/listing/{paragraph_id}``; ``533`` is the publications
listing. It answers JSON — ``{"items": [{"id": uuid, "markup": "…"}], "meta": {...}}``
— where ``markup`` is the rendered teaser and ``meta`` carries the total count and the
full facet vocabulary. The query grammar is not guessable and is not documented; it was
read off the site's own ``filter-listing`` bundle, and all four parts of it are used
here:

``page``       0-indexed, ten per page, **omitted** for the first page.
``fulltext``   the keyword box. Searched at the source, so ``query`` is precise.
``sort[K][path]=K&sort[K][direction]=desc``      ``K`` ∈ ``field_published``,
               ``field_closing_date``, ``title``, ``search_api_relevance``.
``filter[F][path]=<field>&filter[F][value][]=<term id>``   one facet, where ``F`` is
               the facet key and ``<field>`` its backing Drupal field — the mapping in
               :data:`FACET_FIELDS`. Passing the term id without its ``path`` is
               accepted with a 200 and silently ignored, which is the failure mode to
               watch for here: a filtered harvest that quietly walks all 24,059.

**The sort is nearly-sorted, not sorted.** ``field_published`` descending is honoured
to within a few days: page 3 of the unfiltered feed runs 24 → 23 → 21 July and then
lands a 28 July item, and the Decisions facet puts a 15 July item after a 15 June one.
An early-stop that halted at the first item older than the cursor would therefore drop
real documents. Discovery instead stops only after
:data:`STOP_AFTER_STALE_PAGES` **consecutive** pages contained nothing newer than the
watermark, which absorbs jitter an order of magnitude larger than any observed.

**A publication is a landing page plus its files.** The page carries the abstract and
the body; the substance is in the attachments — ``[PDF, 1.70MB]``,
``[DOCX, 107.19KB]``, ``[XLSX, 0.98MB]`` — and all of them are downloaded, extracted
and inlined into the record's text, so a consultation's response template and a price
control's financial model are searchable beside the prose. The page's own
definition-list is the metadata (publication type, publication date, closing date,
status, topic, subtopic, scheme name), read as a ``dt``/``dd`` stream because Topic
renders as one ``dt`` followed by several ``dd``.

The register mixes legally operative material with blogs, speeches and press notices,
so it opts into RagLex's relevance gate: everything is held and deduped, and only
documents that cite a case or an instrument are embedded, listed and searched.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date
from typing import Iterator
from urllib.parse import urljoin, urlsplit, unquote

from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.errors import RateLimitException
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub

BASE = "https://www.ofgem.gov.uk"
LISTING_API = BASE + "/api/listing"
#: The publications listing paragraph. Other listings exist (news, cases); this one is
#: the whole publication corpus, and its facets cover every publication type.
PUBLICATIONS_LISTING = "533"
PUBLICATIONS_PAGE = BASE + "/publications"

#: Facet key → the Drupal field it filters on, from the site's own listing settings.
#: A ``filter[key][path]`` that is not the matching field is ignored, not rejected.
FACET_FIELDS: dict[str, str] = {
    "facet_publication_date": "field_published",
    "facet_industry_sector": "field_industry_sector",
    "topic": "field_topic",
    "facet_scheme_name": "field_scheme_name",
    "facet_case_publication_type": "field_case_publication_type",
}

#: Consecutive result pages with nothing newer than the cursor before discovery stops.
#: See the module docstring: the feed's date order is approximate.
STOP_AFTER_STALE_PAGES = 3

PAGE_SIZE = 10

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) "
                   "Gecko/20100101 Firefox/153.0"),
    "Accept": "*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": PUBLICATIONS_PAGE,
}

#: Publication types whose documents are operative acts rather than commentary.
_DECISION_TYPES = {
    "decision", "enforcement case", "compliance case", "licence granted",
    "licence revocation", "licence modification", "licence transfer",
    "licence application", "licence application refused", "code modification",
}

_ANCHOR = re.compile(r'<a[^>]+href="(/[^"]+)"', re.S)
_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_TEASER_SUMMARY = re.compile(r'<div\s+class="c-wysiwyg[^"]*">(.*?)</div>', re.S)
_TEASER_ROW = re.compile(r'<div>\s*<span class="font-bold">(.*?):</span>(.*?)</div>', re.S)
_TIME = re.compile(r'<time datetime="([^"]+)"')
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
#: The detail page's metadata, as a stream: Topic is one ``dt`` with several ``dd``.
_DEF_ITEM = re.compile(r"<(dt|dd)\b[^>]*>(.*?)</\1>", re.S)
#: ``<a class="media media-default media-publication_document" href="…">…</a>`` —
#: the only ``media`` anchor Ofgem renders on a publication page.
_DOCUMENT = re.compile(r'<a\s+class="(media\s+media-[^"]*)"\s+href="([^"]+)"[^>]*>(.*?)</a>',
                       re.S)
#: The trailing ``[PDF, 252.21KB]`` a document link's label always carries.
_FORMAT = re.compile(r"\[([A-Za-z0-9]+),\s*([^\]]+)\]\s*$")

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def clean(fragment: str | None) -> str:
    """Markup fragment → its collapsed visible text."""
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_date(value: str | None) -> date | None:
    """``2026-07-30T12:00:00Z`` or ``30 July 2026`` → a date."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+((?:19|20)\d{2})\b", text)
    if m and m.group(2).lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    return None


@dataclass(frozen=True, slots=True)
class OfgemTeaser:
    """One row of the listing API, as the teaser markup presents it."""

    uuid: str | None
    href: str                       # site-relative landing path
    title: str
    summary: str | None
    published: str | None           # the ISO stamp from <time datetime=…>
    meta: dict = dataclass_field(default_factory=dict)

    @property
    def date(self) -> date | None:
        return parse_date(self.published or self.meta.get("publication date"))


@dataclass(frozen=True, slots=True)
class OfgemDocument:
    """An attached file on a publication page."""

    url: str
    title: str
    ext: str | None
    size: str | None


def parse_teaser(markup: str, *, uuid: str | None = None) -> OfgemTeaser | None:
    """One item's rendered teaser → its link, title, abstract and facet metadata.

    The metadata rows are read from the whole fragment rather than from a delimited
    ``teaser__meta`` block: the block's own closing ``</div>`` cannot be matched without
    counting nesting, and every attempt to delimit it cost the last rows — Topic and
    Scheme name — which are exactly the two facets worth keeping.
    """
    anchor = _ANCHOR.search(markup or "")
    title = _H3.search(markup or "")
    if not anchor or not title:
        return None
    summary = _TEASER_SUMMARY.search(markup)
    when = _TIME.search(markup)
    return OfgemTeaser(
        uuid=uuid,
        href=anchor.group(1),
        title=clean(title.group(1)),
        summary=clean(summary.group(1)) if summary else None,
        published=when.group(1) if when else None,
        meta={clean(label).lower(): clean(value)
              for label, value in _TEASER_ROW.findall(markup)},
    )


def parse_listing(payload: dict) -> list[OfgemTeaser]:
    """A listing API response → its teasers, in the order the API returned them."""
    out: list[OfgemTeaser] = []
    for item in (payload or {}).get("items") or ():
        if not isinstance(item, dict):
            continue
        teaser = parse_teaser(str(item.get("markup") or ""), uuid=item.get("id"))
        if teaser is not None:
            out.append(teaser)
    return out


def parse_metadata(html: str) -> dict[str, list[str]]:
    """The publication page's definition list → ``{label: [values]}``.

    Read as an ordered ``dt``/``dd`` stream because Ofgem renders a multi-valued facet
    as one ``dt`` followed by several ``dd`` (``Topic:`` → "Electricity transmission,"
    + "National Energy System Operator (NESO)"), which no pairwise match sees whole.
    """
    out: dict[str, list[str]] = {}
    label: str | None = None
    for kind, body in _DEF_ITEM.findall(html or ""):
        text = clean(body)
        if kind == "dt":
            label = clean(body).rstrip(":").lower() or None
            if label:
                out.setdefault(label, [])
        elif label and text:
            out[label].append(text.rstrip(","))
    return {k: v for k, v in out.items() if v}


def parse_documents(html: str) -> list[OfgemDocument]:
    """The files attached to a publication page, in page order, deduplicated."""
    out: list[OfgemDocument] = []
    seen: set[str] = set()
    for _classes, href, body in _DOCUMENT.findall(html or ""):
        url = urljoin(BASE, _html.unescape(href).strip())
        if not url or url in seen:
            continue
        seen.add(url)
        label = clean(body)
        fmt = _FORMAT.search(label)
        ext = (fmt.group(1).lower() if fmt else
               (urlsplit(url).path.rsplit(".", 1)[-1].lower()
                if "." in urlsplit(url).path.rsplit("/", 1)[-1] else None))
        out.append(OfgemDocument(
            url=url,
            title=(_FORMAT.sub("", label).strip() or
                   unquote(urlsplit(url).path.rsplit("/", 1)[-1])),
            ext=ext,
            size=fmt.group(2).strip() if fmt else None,
        ))
    return out


def page_text(html: str) -> str:
    """The readable body of a publication page.

    Ofgem's pages are ~190 KB of Tailwind chrome around a few KB of prose, so the
    navigation, the share block and the print button are removed by selector rather
    than filtered out of the text afterwards.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one("main") or soup
    for selector in ("script", "style", "svg", "noscript", "nav", "form",
                     "header", "footer", "#block-numiko-socialshareblock",
                     "[data-js-print]"):
        for tag in main.select(selector):
            tag.decompose()
    lines = [" ".join(part.split()) for part in main.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def stable_id(href: str) -> str:
    """``/decision/foo`` → ``ofgem/decision/foo`` — the site's own path is the key."""
    path = urlsplit(href).path.strip("/")
    return "ofgem/" + re.sub(r"[^a-z0-9/_-]+", "-", path.lower()).strip("-/")


def doc_type_for(publication_type: str | None) -> DocType:
    return (DocType.DECISION if (publication_type or "").strip().lower() in _DECISION_TYPES
            else DocType.GUIDANCE)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


class OfgemPublicationsAdapter(BaseAdapter):
    source = "uk-ofgem-publications"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        listing_id: str | None = None,
        query: str | None = None,
        facet: str | None = None,
        facet_value: str | None = None,
        include_documents: bool = True,
        max_documents: int = 20,
        require_recognized_legal_citation: bool = True,
        start_offset: int = 0,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.listing_id = str(listing_id or PUBLICATIONS_LISTING).strip()
        self.query = (query or "").strip() or None
        facet = (facet or "").strip() or None
        if facet and facet not in FACET_FIELDS:
            raise ValueError(
                f"unknown Ofgem facet {facet!r}; one of {sorted(FACET_FIELDS)}")
        self.facet = facet
        self.facet_value = (str(facet_value).strip() if facet_value else None)
        self.include_documents = option_flag(include_documents, True)
        self.max_documents = max(0, option_int(max_documents, 20))
        self.require_recognized_legal_citation = option_flag(
            require_recognized_legal_citation, True)
        # An adapter that REPORTS ``resume_offset`` must also accept it back. An
        # interrupted harvest is resumed with ``options["start_offset"]`` set from the
        # checkpoint, and ``get_adapter`` passes options straight to the constructor —
        # so an adapter without this parameter does not merely restart from the top, it
        # raises TypeError and the resume fails outright. 24,059 publications is exactly
        # the length of walk that will be interrupted at least once.
        self.start_offset = max(0, option_int(start_offset, 0))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    # ---- discovery -------------------------------------------------------------------

    def _listing_url(self, page: int) -> str:
        """The listing request for a 1-based page, in the site's own query grammar."""
        parts = [
            "sort[field_published][path]=field_published",
            "sort[field_published][direction]=desc",
        ]
        if page > 1:                       # the API's page is 0-indexed and omitted at 1
            parts.append(f"page={page - 1}")
        if self.query:
            from urllib.parse import quote_plus

            parts.append(f"fulltext={quote_plus(self.query)}")
        if self.facet and self.facet_value:
            from urllib.parse import quote_plus

            parts.insert(0, f"filter[{self.facet}][path]={FACET_FIELDS[self.facet]}")
            parts.insert(1, f"filter[{self.facet}][value][]="
                            f"{quote_plus(self.facet_value)}")
        return f"{LISTING_API}/{self.listing_id}?" + "&".join(parts)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        cutoff = (since or "").strip() or None
        # Resume where the interrupted run left off, not at the top of the register.
        first_page = self.start_offset // PAGE_SIZE
        page = first_page
        walked = 0
        stale_pages = 0
        total: int | None = None
        while True:
            page += 1
            walked += 1
            if max_pages is not None and walked > max_pages:
                return
            payload = self._client.get(self._listing_url(page),
                                       headers=_HEADERS).json()
            teasers = parse_listing(payload)
            if not teasers:
                return
            if total is None:
                total = int(((payload.get("meta") or {}).get("count")) or 0) or None
            fresh = 0
            for teaser in teasers:
                published = teaser.published or ""
                if cutoff and published and published <= cutoff:
                    continue
                fresh += 1
                yield Stub(
                    stable_id=stable_id(teaser.href),
                    landing_url=urljoin(BASE, teaser.href),
                    raw_url=urljoin(BASE, teaser.href),
                    title=teaser.title,
                    court="Ofgem",
                    hint_date=teaser.date,
                    hints={
                        "teaser": teaser,
                        "uuid": teaser.uuid,
                        # ``field_published`` is the only stamp the listing carries and
                        # it does NOT move when a page is edited, so it is a watermark
                        # and deliberately not a ``contenthash``: using it as one would
                        # suppress the re-fetch of a revised publication forever.
                        "watermark": published or None,
                        "summary": teaser.summary,
                        "publication_type": teaser.meta.get("publication type"),
                        "feed_total": total,
                        "resume_offset": (page - 1) * PAGE_SIZE,
                    },
                )
            # The feed's date order is approximate (see the module docstring), so one
            # exhausted page is not the end of the new material — several in a row are.
            stale_pages = 0 if fresh else stale_pages + 1
            if cutoff and stale_pages >= STOP_AFTER_STALE_PAGES:
                return
            if total is not None and page * PAGE_SIZE >= total:
                return
            if len(teasers) < PAGE_SIZE:
                return

    # ---- fetch -----------------------------------------------------------------------

    def _attachment_text(self, doc: OfgemDocument) -> tuple[str, dict]:
        """One attachment's extracted text and its metadata row.

        A file that 404s or that the extractor chokes on costs that attachment, never
        the publication — but a rate limit must reach the pipeline so it can pause the
        queue rather than hammering on through the register.

        Ofgem attaches spreadsheets and models beside its PDFs (a price control's PCFM
        is an XLSX), and those have no text engine. They are recorded as attachments
        and left unread rather than byte-decoded into megabytes of zip noise.
        """
        from ..extraction import extract_bytes, text_extension

        meta = {"url": doc.url, "title": doc.title, "format": doc.ext, "size": doc.size}
        readable = text_extension(doc.ext)
        if readable is None:
            return "", {**meta, "skipped": "no-text-engine"}
        try:
            body = self._client.get(doc.url, headers=_HEADERS).content or b""
        except RateLimitException:
            raise
        except Exception:                       # noqa: BLE001 — see docstring
            return "", {**meta, "skipped": "unavailable"}
        try:
            extracted = extract_bytes(body, ext=readable)
        except Exception:                       # noqa: BLE001
            return "", {**meta, "bytes": len(body), "skipped": "extraction-failed"}
        text = (extracted.text or "").strip()
        if not text and readable == "pdf":
            from ..extraction.ocr import text_or_ocr

            text = (text_or_ocr(body)[0] or "").strip()
        return text, {**meta, "bytes": len(body), "text_chars": len(text)}

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url or stub.landing_url, headers=_HEADERS)
        raw = response.content or b""
        html = raw.decode("utf-8", "replace")
        teaser: OfgemTeaser | None = stub.hints.get("teaser")
        heading = _H1.search(html)
        title = (clean(heading.group(1)) if heading else "") or stub.title or (
            teaser.title if teaser else None)
        meta = parse_metadata(html)

        def one(label: str) -> str | None:
            values = meta.get(label) or []
            return values[0] if values else None

        publication_type = one("publication type") or stub.hints.get("publication_type")
        published = parse_date(one("publication date")) or stub.hint_date
        summary = stub.hints.get("summary") or (teaser.summary if teaser else None)

        parts = [text for text in (title, summary, page_text(html)) if text]
        attachments: list[dict] = []
        if self.include_documents:
            documents = parse_documents(html)
            if self.max_documents:          # 0 means "however many the page carries"
                documents = documents[: self.max_documents]
            for doc in documents:
                text, row = self._attachment_text(doc)
                attachments.append(row)
                if text:
                    parts.append(f"{doc.title}\n{text}")
        text = "\n\n".join(dict.fromkeys(parts)).strip()
        if len(text) < 40:
            return None

        topics = meta.get("topic") or []
        subtopics = meta.get("subtopic") or []
        schemes = meta.get("scheme name") or []
        tags = ["ofgem", "energy", "regulatory",
                *([slugify(publication_type)] if publication_type else []),
                *(slugify(t) for t in topics + subtopics + schemes)]
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=doc_type_for(publication_type),
            title=title,
            court="Ofgem",
            decision_date=published,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="html",
            text=text,
            extracted_via=ExtractedVia.SCRAPE,
            topic_tags=[t for t in dict.fromkeys(tags) if t],
            extra={k: v for k, v in {
                "jurisdiction": "gb",
                "issuer": "Ofgem",
                "uuid": stub.hints.get("uuid"),
                "publication_type": publication_type,
                "status": one("status"),
                "closing_date": one("closing date"),
                "topics": topics,
                "subtopics": subtopics,
                "schemes": schemes,
                "summary": summary,
                "published": published.isoformat() if published else None,
                "attachments": attachments,
                "licence": "Crown copyright",
                "require_recognized_legal_citation":
                    self.require_recognized_legal_citation,
            }.items() if v not in (None, [], "")},
        )


__all__ = [
    "FACET_FIELDS",
    "OfgemDocument",
    "OfgemPublicationsAdapter",
    "OfgemTeaser",
    "PUBLICATIONS_LISTING",
    "STOP_AFTER_STALE_PAGES",
    "parse_documents",
    "parse_listing",
    "parse_metadata",
    "parse_teaser",
    "page_text",
    "stable_id",
]
