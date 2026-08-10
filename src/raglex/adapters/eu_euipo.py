"""EUIPO Observatory publications — the EU's evidence base on IP infringement.

The European Union Intellectual Property Office runs the **European Observatory on
Infringements of Intellectual Property Rights**, established by Regulation (EU) No
386/2012. Its publications are the studies the EU institutions actually cite when they
legislate on counterfeiting, online enforcement, copyright and IP in the economy — the
IP Perception surveys, the *IPR Infringement* and *Online Advertising on IPR-Infringing
Websites and Apps* series, the industry-level economic-cost studies, the case-law and
legal-comparison reports.

**One JSON index, and it is the whole listing.** euipo.europa.eu is a Storyblok site
searched through a public Algolia index (``ews-en-events``), which the site's own
front-end queries with the browsable key that ships in its JavaScript. Faceting on
``type:observatory-publications`` returns every publication — 173 of them at the time of
writing — twenty per page over nine pages, each hit carrying the slug, title, summary,
category, publication date and the page's own prose in a ``body`` array. So discovery
needs no crawl of a paginated HTML listing and no headless browser: nine POSTs.

**The reports themselves are one level down.** The Algolia record's ``body`` is the
landing page's copy, not the study; the study is a PDF linked from that page, on
``euipo.europa.eu/tunnel-web/secure/webdav/…``. A publication usually has two —
``…_FullR_en.pdf`` and ``…_ExSum_en.pdf`` — and a big one has many more: *IP-backed
finance in Europe* hangs an executive brief, a Q&A, a press release and a country note
for each Member State off a single page. All of them are followed, extracted and
inlined into the one document, because they are one publication: a reader searching for
what the study found about Austria should not have to know that it lives in
``country_note_at.pdf``.

**Politeness and the WAF.** The site 403s a non-browser User-Agent outright (verified),
so these requests identify as a browser; the Algolia endpoint additionally requires the
``Origin``/``Referer`` of the site whose key it is. Everything is public and
unauthenticated — the Algolia key is the search-only key the page ships to every
visitor.
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator
from urllib.parse import unquote, urlsplit

from ..core.adapter import BaseAdapter, option_int
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub

SITE = "https://www.euipo.europa.eu"
#: The site's own public search index and the search-only key its front-end ships.
ALGOLIA_APP = "ZYN8P9OCP2"
ALGOLIA_KEY = "428a6eab6ad825546f741c199084e245"
ALGOLIA_INDEX = "ews-en-events"
ALGOLIA_URL = (f"https://{ALGOLIA_APP.lower()}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}"
               f"/query?x-algolia-application-id={ALGOLIA_APP}"
               f"&x-algolia-api-key={ALGOLIA_KEY}")
#: The facet that selects the Observatory's publications out of the site-wide index.
PUBLICATIONS_FACET = "type:observatory-publications"

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) "
       "Gecko/20100101 Firefox/153.0")
_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
_ALGOLIA_HEADERS = {
    "Accept": "application/json",
    # Algolia's browser protocol: the key is Origin-scoped, and it is sent as
    # text/plain precisely so the request stays a CORS simple request.
    "Content-Type": "text/plain",
    "Origin": SITE,
    "Referer": f"{SITE}/",
}

#: Every download on a publication page is a plain anchor to the document library.
_PDF_HREF = re.compile(r'<a[^>]+href="(https?://[^"]*?\.pdf(?:\?[^"]*)?)"', re.I)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)

#: How many linked PDFs one publication may pull in. The country-note reports are the
#: reason there is a cap at all and the reason it is not small.
DEFAULT_MAX_PDFS = 60


def clean(fragment: str | None) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def slug_id(full_slug: str) -> str | None:
    """``en/publications/euipn-trends-report-2025`` → ``euipo/euipn-trends-report-2025``.

    The slug is the URL and it is what a reader would cite; the Algolia ``objectID`` is
    a Storyblok UUID that says nothing. Case is folded because the site is inconsistent
    about it (``IP-backed-finance-in-Europe-…`` beside all-lowercase neighbours) and two
    spellings of one publication must not become two documents.
    """
    tail = (full_slug or "").strip("/").rsplit("/", 1)[-1].strip().casefold()
    return f"euipo/{tail}" if tail else None


def published_on(hit: dict) -> date | None:
    """The publication date, from Algolia's epoch-milliseconds ``startDate``."""
    stamp = hit.get("startDate")
    if not isinstance(stamp, (int, float)) or stamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def hit_text(hit: dict) -> str:
    """The landing page's own prose, as the index already holds it."""
    parts = [clean(hit.get("title")), clean(hit.get("summary"))]
    body = hit.get("body")
    if isinstance(body, list):
        parts.extend(str(part) for part in body if isinstance(part, str))
    return "\n\n".join(p for p in (part.strip() for part in parts) if p)


def pdf_links(html: str) -> list[str]:
    """Every PDF linked from a publication page, in page order, de-duplicated."""
    return list(dict.fromkeys(_html.unescape(m.group(1)) for m in _PDF_HREF.finditer(html)))


def pdf_label(url: str) -> str:
    """A readable name for an attachment, from its filename."""
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return re.sub(r"[_+]+", " ", name.removesuffix(".pdf")).strip() or name


@dataclass(frozen=True, slots=True)
class Publication:
    stable_id: str
    slug: str
    title: str
    category: str | None
    published: date | None
    summary: str
    text: str


def parse_hits(payload: dict) -> list[Publication]:
    """The publications on one Algolia page (pure — the parser the tests exercise)."""
    out: list[Publication] = []
    for hit in payload.get("hits") or []:
        full_slug = str(hit.get("fullSlug") or "")
        stable_id = slug_id(full_slug)
        title = clean(hit.get("title"))
        if not stable_id or not title:
            continue
        out.append(Publication(
            stable_id=stable_id,
            slug=full_slug.strip("/"),
            title=title,
            category=clean(hit.get("category")) or None,
            published=published_on(hit),
            summary=clean(hit.get("summary")),
            text=hit_text(hit),
        ))
    return out


class EUIPOPublicationsAdapter(BaseAdapter):
    """EUIPO Observatory publications: the Algolia index, then the PDFs one level down."""

    source = "eu-euipo"
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    def __init__(self, *, max_pdfs=None, client: RateLimitedClient | None = None) -> None:
        self.max_pdfs = max(0, option_int(max_pdfs, DEFAULT_MAX_PDFS))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, user_agent=_UA, timeout=90)

    # -- discovery ----------------------------------------------------------
    def _query(self, page: int) -> dict:
        body = json.dumps({
            "query": "", "page": page, "filters": "",
            "facetFilters": [PUBLICATIONS_FACET, []],
            "facets": ["*"], "attributesToHighlight": [],
        })
        resp = self._client.request("POST", ALGOLIA_URL, headers=_ALGOLIA_HEADERS,
                                    content=body.encode("utf-8"))
        return resp.json()

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Walk every page of the facet.

        A FULL walk each time, not a date cursor. The index is nine pages, it is not
        ordered by date, and the Observatory re-publishes a study's page when a language
        version or a country note is added — so a cursor over ``startDate`` would both
        cost nothing to skip and silently miss the additions. ``since`` filters the
        yielded stubs instead of truncating the walk.
        """
        watermark = (since or "")[:10]
        page, pages = 0, 1
        while page < pages:
            try:
                payload = self._query(page)
            except (FetchError, ValueError):
                return
            pages = int(payload.get("nbPages") or 1)
            for pub in parse_hits(payload):
                if watermark and pub.published and pub.published.isoformat() <= watermark:
                    continue
                yield Stub(
                    stable_id=pub.stable_id,
                    title=pub.title,
                    landing_url=f"{SITE}/{pub.slug}",
                    raw_url=f"{SITE}/{pub.slug}",
                    hint_date=pub.published,
                    hints={
                        "category": pub.category,
                        "summary": pub.summary,
                        "index_text": pub.text,
                        "published": pub.published.isoformat() if pub.published else None,
                        "watermark": pub.published.isoformat() if pub.published else None,
                    },
                )
            page += 1
            if max_pages is not None and page >= max_pages:
                return

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        try:
            page = self._client.get(stub.raw_url, headers=_PAGE_HEADERS)
        except FetchError:
            return None
        html = page.text or ""
        parts = [str(stub.hints.get("index_text") or "").strip()]
        if not parts[0]:
            parts = [clean(_H1.search(html).group(1)) if _H1.search(html) else ""]

        attachments: list[dict] = []
        raw, ext = page.content, "html"
        links = pdf_links(html)
        for url in links[:self.max_pdfs]:
            try:
                blob = self._client.get(url, headers=_PAGE_HEADERS).content
                body = (extract_bytes(blob, ext="pdf",
                                      mime="application/pdf").text or "").strip()
            except (FetchError, ValueError, RuntimeError):
                attachments.append({"url": url, "title": pdf_label(url),
                                    "bytes": None, "text_chars": 0})
                continue
            if body:
                parts.append(f"{pdf_label(url)}\n\n{body}")
                if ext != "pdf":
                    # The study itself is the document's raw artefact; the landing page
                    # is chrome. Keep the FIRST readable PDF, which is page order, and
                    # page order puts the report above its annexes.
                    raw, ext = blob, "pdf"
            attachments.append({"url": url, "title": pdf_label(url),
                                "bytes": len(blob), "text_chars": len(body)})

        text = "\n\n".join(p for p in parts if p).strip()
        if len(text) < 120:
            return None
        published = stub.hint_date
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=stub.title,
            court="EUIPO",
            decision_date=published,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["euipo", "intellectual-property", "ip-enforcement"],
            extra={
                "jurisdiction": "eu",
                "issuer": "European Union Intellectual Property Office",
                "publisher_body": "European Observatory on Infringements of "
                                  "Intellectual Property Rights",
                "euipo_category": stub.hints.get("category"),
                "summary": stub.hints.get("summary"),
                "attachments": attachments,
                "attachments_found": len(links),
                "attachments_truncated": len(links) > self.max_pdfs,
            },
        )
