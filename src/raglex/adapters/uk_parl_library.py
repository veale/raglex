"""The Commons and Lords Library research briefings — whose RSS feed *is* the corpus.

Both Libraries publish their research on WordPress, and both put the **whole briefing**
in ``content:encoded``. That is not the usual RSS teaser: CBP-10974 arrives from the feed
with 11,599 characters of text under the same eight ``<h2>`` headings the web page shows,
and the only thing the page adds is its "Related posts" trailer. So the feed is not a
discovery hint that then costs one page fetch per document — it is ten complete documents
per request.

**And it paginates all the way to the beginning.** ``?paged=N`` on a WordPress feed is
not documented anywhere on either site, but it works: the Commons feed runs to page 1,200
(12,000 items, back to a 1993 research paper) and the Lords feed to page 281 (back to
1998), and one page past the end returns a well-formed feed with no ``<item>`` in it,
which is an unambiguous stop. A full Commons backfill is therefore ~1,200 requests rather
than ~12,000, and an incremental run is one.

**The feed must be fetched as bytes, not as rendered HTML.** Everything on
``parliament.uk`` sits behind a Cloudflare managed challenge that refuses a plain client
with a 403 interstitial, so the fetch goes through the browser — and a browser handed an
RSS URL parses it as *HTML*, where ``<link>`` is a void element. Read that way, every
item's ``<link>`` swallows the rest of the item and the guids come back pointing at
``local.parliament.uk``, the Libraries' staging host. Taking the navigation response's
body instead gives the raw XML, which parses properly and whose guids are canonical.

**A thin ``content:encoded`` means the briefing is a PDF**, not that it is short. Before
the Libraries published in HTML the briefing was a typeset paper and the feed carries only
its abstract — 91 characters for Research Paper 93/1, against 18,201 for a 2026 briefing.
Below :data:`FULL_TEXT_FLOOR` this adapter goes to the document page for the
``researchbriefings.files.parliament.uk`` PDF, which is fetched by navigating to it from
the briefing page (a direct hit is refused whatever cookie it carries — see
``BrowserBytesFetcher``). The oldest of those are scans with no text layer at all, so they
go through OCR rather than being recorded as empty.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import date, datetime, timezone
from typing import Iterator

from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub

log = logging.getLogger("raglex.adapters.uk_parl_library")

HOUSES: dict[str, dict] = {
    "commons": {
        "source": "uk-commons-library",
        "host": "https://commonslibrary.parliament.uk",
        "listing": "https://commonslibrary.parliament.uk/briefings/all-research/",
        "feed": "https://commonslibrary.parliament.uk/briefings/all-research/feed/",
        "issuer": "House of Commons Library",
        "id_prefix": "uk/commons-library",
    },
    "lords": {
        "source": "uk-lords-library",
        "host": "https://lordslibrary.parliament.uk",
        "listing": "https://lordslibrary.parliament.uk/research/all-research/",
        "feed": "https://lordslibrary.parliament.uk/research/all-research/feed/",
        "issuer": "House of Lords Library",
        "id_prefix": "uk/lords-library",
    },
}

#: Below this many characters of body text, ``content:encoded`` is an abstract standing in
#: for a PDF rather than the briefing itself. Measured: the shortest genuinely-HTML
#: briefings sampled run to ~2,000 characters; the PDF-backed stubs to ~120.
FULL_TEXT_FLOOR = 600

#: Where both Houses keep the typeset papers, under the briefing's own id in upper case.
PDF_HOST = "researchbriefings.files.parliament.uk"
_PDF_LINK = re.compile(
    rf'href="(https?://{re.escape(PDF_HOST)}/[^"]+\.pdf)"', re.I)
#: ``rp94-22``, ``cbp-10974``, ``sn02811``, ``lln-2019-0042`` — a series prefix and its
#: numbers. Everything else is a prose permalink, which names no file.
_BRIEFING_NUMBER = re.compile(r"[a-z]{2,4}[-_]?\d{1,6}(?:[-_]\d{1,4})*", re.I)

#: The Libraries' staging host leaks into some guids. It is the same document.
_STAGING = re.compile(r"https?://local\.parliament\.uk", re.I)

#: A briefing page's own header. Both Libraries run the same theme, so one set of
#: patterns serves them: the ``<time datetime=…>`` is machine-readable and is the
#: publication date, not the RSS post date.
_PAGE_TITLE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_PAGE_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"', re.I)
_PAGE_AUTHOR = re.compile(r'<a[^>]+href="[^"]*/authors/([^"/]+)/"[^>]*>(.*?)</a>', re.S)
_PAGE_TOPIC = re.compile(
    r'<a[^>]+href="[^"]*/topic/[^"]*"[^>]*>\s*<span class="tag__label">(.*?)</span>', re.S)
_PAGE_TYPE = re.compile(
    r'<a[^>]+href="[^"]*/type/[^"]*"[^>]*>\s*<span class="tag__label">(.*?)</span>', re.S)

_ATOM = {"content": "http://purl.org/rss/1.0/modules/content/",
         "dc": "http://purl.org/dc/elements/1.1/"}


def _clean(fragment: str) -> str:
    import html as _html

    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def canonical_url(url: str, host: str) -> str:
    """A staging-host link is the same briefing on the public host."""
    return _STAGING.sub(host.rstrip("/"), (url or "").strip())


def slug_of(url: str) -> str | None:
    """``…/research-briefings/cbp-10974/`` → ``cbp-10974``; a Lords briefing published
    without the ``/research-briefings/`` prefix is keyed on its own path slug."""
    path = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not path:
        return None
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3:          # scheme, host and nothing else
        return None
    return parts[-1] or None


def parse_pub_date(value: str | None) -> date | None:
    """RFC-822 (``Fri, 07 Aug 2026 10:38:03 +0000``), which is what both feeds emit."""
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.date()


def parse_feed(raw: bytes, *, host: str) -> list[dict]:
    """One feed page → its items, in feed order (newest first).

    Returns ``[]`` for a well-formed feed with no items, which is how the end of the
    archive announces itself, and also for markup that will not parse at all — the
    caller cannot tell those apart and must not, because both mean "stop here"."""
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        log.warning("uk-parl-library: feed page did not parse as XML (%d bytes)", len(raw))
        return []
    out: list[dict] = []
    for item in root.iter("item"):
        link = canonical_url((item.findtext("link") or "").strip(), host)
        guid = canonical_url((item.findtext("guid") or "").strip(), host)
        # The Lords guid is a bare ``?p=28885``; the link is the addressable document.
        url = link or guid
        if not url or host.split("//", 1)[-1] not in url:
            continue        # a blog cross-post from another host is not a briefing
        body = item.findtext(f"{{{_ATOM['content']}}}encoded") or ""
        out.append({
            "url": url,
            "slug": slug_of(url),
            "title": _clean(item.findtext("title") or "") or None,
            "date": parse_pub_date(item.findtext("pubDate")),
            "summary": _clean(item.findtext("description") or "") or None,
            "author": _clean(item.findtext(f"{{{_ATOM['dc']}}}creator") or "") or None,
            "topics": [_clean(c.text or "") for c in item.findall("category")
                       if _clean(c.text or "")],
            "content_html": body,
        })
    return out


def html_body(fragment: str) -> tuple[str, list[Segment]]:
    """A briefing's ``content:encoded`` → flat text plus one segment per heading.

    The headings are the briefing's own section structure and are what a pinpoint
    citation into a Library briefing would name, so they are kept as citable segments
    rather than being flattened into prose."""
    if not (fragment or "").strip():
        return "", []
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a core dependency
        return _clean(fragment), []
    soup = BeautifulSoup(fragment, "html.parser")
    for node in soup(["script", "style"]):
        node.decompose()
    parts: list[str] = []
    headings: list[tuple[str, int, int]] = []
    cursor = 0
    for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "blockquote"]):
        if node.name in {"p", "blockquote"} and node.find_parent("li") is not None:
            continue
        if node.name == "li":
            own = " ".join(str(child) for child in node.contents
                           if getattr(child, "name", None) not in {"ul", "ol"})
            value = _clean(BeautifulSoup(own, "html.parser").get_text(" ", strip=True))
        else:
            value = _clean(node.get_text(" ", strip=True))
        if not value:
            continue
        if parts:
            cursor += 2
        start = cursor
        parts.append(value)
        cursor += len(value)
        if node.name in {"h1", "h2", "h3", "h4"}:
            headings.append((value, start, int(node.name[1])))
    text = "\n\n".join(parts)
    segments: list[Segment] = []
    for index, (label, start, level) in enumerate(headings):
        end = headings[index + 1][1] - 2 if index + 1 < len(headings) else len(text)
        segments.append(Segment(label=label, char_start=start, char_end=max(start, end),
                                kind="section", level=max(0, level - 2)))
    return text, segments


def page_body(html: str) -> tuple[str, list[Segment]]:
    """A briefing page's prose, for the one case the feed cannot serve.

    Both Library themes render the body as a run of ``div.component--text`` blocks, each
    wrapping a ``div.reading-width``, and nothing else on the page uses that pair. This
    is only reached for a briefing fetched **by id** whose feed row we never saw: without
    it a targeted fetch of a modern HTML-only briefing found no PDF to fall back to
    either, and returned nothing at all."""
    if not (html or "").strip():
        return "", []
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a core dependency
        return "", []
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select("div.component--text div.reading-width") or \
        soup.select("div.component--text")
    if not blocks:
        return "", []
    return html_body("".join(str(block) for block in blocks))


def parse_page(html: str) -> dict:
    """Title, date, authors, topics and publication type from a briefing's own page.

    The feed already supplies most of this on the ordinary path, so this exists for the
    two cases where there is no feed row: a briefing fetched by id, and one whose feed
    body was an abstract and whose page we are visiting for the PDF anyway. Without it a
    targeted fetch is stored titled ``RP94-22`` and undated, which is exactly how the
    document becomes unfindable by the name anybody would look for it under."""
    html = html or ""
    title = _PAGE_TITLE.search(html)
    when = _PAGE_TIME.search(html)
    return {
        "title": _clean(title.group(1)) if title else None,
        "date": _iso_day(when.group(1)) if when else None,
        "authors": [_clean(name) for _href, name in _PAGE_AUTHOR.findall(html)
                    if _clean(name)],
        "topics": list(dict.fromkeys(
            _clean(label) for label in _PAGE_TOPIC.findall(html) if _clean(label))),
        "publication_type": (_clean(_PAGE_TYPE.search(html).group(1))
                             if _PAGE_TYPE.search(html) else None),
    }


def _iso_day(value: str | None) -> date | None:
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def pdf_url_for(page_html: str, slug: str | None) -> str | None:
    """The briefing's own PDF on the research-briefings file host.

    The URL is derivable from the id (``rp94-22`` → ``…/documents/RP94-22/RP94-22.pdf``)
    and that pattern held for everything sampled, but it is only used as a fallback:
    a link the page actually publishes cannot be wrong about a renamed file."""
    found = _PDF_LINK.search(page_html or "")
    if found:
        return found.group(1)
    if slug and _BRIEFING_NUMBER.fullmatch(slug):
        # Only a briefing NUMBER derives a filename. Deriving one from a prose slug
        # ("reducing-gambling-harm-among-young-people") guesses a URL that cannot
        # exist, and pays a browser navigation to be told so.
        upper = slug.upper()
        return f"https://{PDF_HOST}/documents/{upper}/{upper}.pdf"
    return None


class ParliamentLibraryAdapter(BaseAdapter):
    """``house="commons"`` (``uk-commons-library``) or ``house="lords"``
    (``uk-lords-library``)."""

    source = "uk-commons-library"
    #: Every request is a browser page load through a Cloudflare challenge; this is a
    #: courtesy crawl of a public institution, not an API with a published budget.
    min_interval = 2.0
    requires_js = True

    def __init__(self, *, house: str = "commons", start_page: int = 1,
                 max_feed_pages: int = 2000, slugs: str | None = None,
                 include_pdf: bool = True, ocr: bool = True) -> None:
        key = (house or "commons").strip().lower()
        if key not in HOUSES:
            raise ValueError(f"house must be one of {sorted(HOUSES)}, not {house!r}")
        self.house = key
        cfg = HOUSES[key]
        self.source = cfg["source"]
        self.host = cfg["host"]
        self.listing = cfg["listing"]
        self.feed = cfg["feed"]
        self.issuer = cfg["issuer"]
        self.id_prefix = cfg["id_prefix"]
        self.start_page = max(1, option_int(start_page, 1))
        self.max_feed_pages = max(1, option_int(max_feed_pages, 2000))
        self.include_pdf = option_flag(include_pdf, True)
        self.ocr = option_flag(ocr, True)
        self.slugs = tuple(s.strip().lower() for s in str(slugs).split(",") if s.strip()) \
            if slugs else ()
        # Discovery already holds every document's full text; carrying it to ``fetch``
        # in ``Stub.hints`` would pin ~20 KB per stub for a 12,000-item backfill, so it
        # is cached here instead, bounded, and re-read from its feed page on a miss.
        self._items: "OrderedDict[str, dict]" = OrderedDict()

    # ---- plumbing ------------------------------------------------------------------

    def _browser(self):
        from ..scraping.fetcher import get_bytes_fetcher

        return get_bytes_fetcher()

    def _bytes(self, url: str, *, referer: str | None = None) -> bytes | None:
        browser = self._browser()
        if not browser.available():
            log.warning("%s: no browser in this image; cannot read %s", self.source, url)
            return None
        return browser.fetch_bytes(url, referer_url=referer or self.listing)

    def feed_page_url(self, page: int) -> str:
        return self.feed if page <= 1 else f"{self.feed}?paged={page}"

    def _remember(self, item: dict) -> None:
        self._items[item["url"]] = item
        while len(self._items) > 400:
            self._items.popitem(last=False)

    def stable_id(self, slug: str) -> str:
        return f"{self.id_prefix}/{slug}"

    def candidate_urls(self, slug: str) -> list[str]:
        """Where a briefing with this id might live.

        Numbered briefings sit under ``/research-briefings/``, but the Lords have
        published under the bare slug since about 2021 — ``/reducing-gambling-harm-among-
        young-people/`` has no prefix at all. A targeted fetch that assumed the prefix
        found a 404 and reported the briefing as unavailable."""
        return [f"{self.host}/research-briefings/{slug}/", f"{self.host}/{slug}/"]

    # ---- discovery -----------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.slugs:
            for slug in self.slugs:
                url = self.candidate_urls(slug)[0]
                yield Stub(stable_id=self.stable_id(slug), landing_url=url, raw_url=url,
                           hints={"slug": slug, "candidates": self.candidate_urls(slug)})
            return
        cutoff = _as_date(since)
        seen: set[str] = set()
        walked = 0
        for page in range(self.start_page, self.start_page + self.max_feed_pages):
            raw = self._bytes(self.feed_page_url(page))
            rows = parse_feed(raw or b"", host=self.host)
            if not rows:
                return          # a feed page with no <item> IS the end of the archive
            for row in rows:
                url, slug = row["url"], row["slug"]
                if not slug or url in seen:
                    continue
                seen.add(url)
                when = row["date"]
                if cutoff and when and when < cutoff:
                    # Newest-first by post date, so the cursor is reached within a page
                    # or two — but a re-published older briefing can sit above it, so
                    # skip the item rather than abandoning the page.
                    continue
                self._remember(row)
                yield Stub(
                    stable_id=self.stable_id(slug),
                    landing_url=url, raw_url=url,
                    title=row["title"], hint_date=when,
                    hints={"slug": slug, "feed_page": page, "resume_offset": page,
                           "watermark": when.isoformat() if when else None},
                )
            walked += 1
            if max_pages is not None and walked >= max_pages:
                return

    # ---- fetch ---------------------------------------------------------------------

    def _item_for(self, stub: Stub) -> dict | None:
        """The feed row for this stub — from the discovery cache, or by re-reading the
        one feed page it came from."""
        url = stub.landing_url or stub.raw_url or ""
        cached = self._items.get(url)
        if cached is not None:
            return cached
        page = stub.hints.get("feed_page")
        if page:
            for row in parse_feed(self._bytes(self.feed_page_url(int(page))) or b"",
                                  host=self.host):
                self._remember(row)
            if url in self._items:
                return self._items[url]
        return None

    def _page(self, stub: Stub, landing: str | None) -> tuple[bytes | None, str | None]:
        """The briefing's own page, and the URL that actually served it.

        A stub from the feed carries the real permalink and is tried alone. Only a
        targeted fetch has to guess, and it reports back which guess worked so the
        record's ``landing_url`` is a link that resolves."""
        for url in ([landing] if landing and not stub.hints.get("candidates")
                    else list(stub.hints.get("candidates") or [landing])):
            if not url:
                continue
            page = self._bytes(url)
            if page and b"<h1" in page:
                return page, url
        return None, landing

    def fetch(self, stub: Stub) -> Record | None:
        item = self._item_for(stub) or {}
        landing = stub.landing_url or item.get("url") or stub.raw_url
        slug = stub.hints.get("slug") or item.get("slug") or slug_of(landing or "")
        text, segments = html_body(item.get("content_html") or "")
        raw: bytes | None = (item.get("content_html") or "").encode("utf-8") or None
        raw_ext, fmt, needs_ocr, pdf_url = "html", "rss", False, None
        page_html = ""

        if len(text) < FULL_TEXT_FLOOR and self.include_pdf:
            # A stub-length body means the briefing is a typeset paper, not a short one.
            page_bytes, landing = self._page(stub, landing)
            page_html = (page_bytes or b"").decode("utf-8", "ignore")
            pdf_url = pdf_url_for(page_html, slug)
            blob = self._bytes(pdf_url, referer=landing) if pdf_url else None
            if blob and blob[:5] == b"%PDF-":
                from ..extraction.ocr import text_or_ocr

                pdf_text, needs_ocr, spans, engine = text_or_ocr(
                    blob, max_pages=200 if self.ocr else 0)
                if len(pdf_text) > len(text):
                    text = pdf_text
                    segments = [Segment(label=f"p. {page}", char_start=start,
                                        char_end=end, kind="page")
                                for page, start, end in spans]
                    raw, raw_ext, fmt = blob, "pdf", engine
            if not text.strip() and page_bytes:
                # No PDF either: an HTML-only briefing reached by id, whose feed row we
                # never saw. Its prose is on the page.
                body_text, body_segments = page_body(page_html)
                if body_text:
                    text, segments = body_text, body_segments
                    raw, raw_ext, fmt = page_bytes, "html", "page"

        if not text.strip():
            return None

        # The page's own header is authoritative and is the ONLY source of title and date
        # for a briefing reached by id rather than through the feed — without it every
        # targeted fetch was stored as "RP94-22", undated and untagged.
        page = parse_page(page_html) if page_html else {}
        when = page.get("date") or item.get("date") or stub.hint_date
        title = (item.get("title") or stub.title or page.get("title")
                 or (slug or "").upper())
        topics = list(dict.fromkeys(
            list(item.get("topics") or []) + list(page.get("topics") or [])))
        authors = list(dict.fromkeys(
            [a for a in [item.get("author")] if a] + list(page.get("authors") or [])))
        extra = {
            "jurisdiction": "uk",
            "issuer": self.issuer,
            "house": self.house,
            "briefing_id": (slug or "").upper() or None,
            "summary": item.get("summary"),
            "authors": authors,
            "topics": topics,
            "publication_type": page.get("publication_type"),
            "format": fmt,
            "pdf_url": pdf_url,
            "needs_ocr": needs_ocr or None,
            "licence": "Open Parliament Licence",
            # Library briefings range over the whole of policy; retain and dedup every
            # one, but only surface those the grammars recognise as legal material.
            "require_recognized_legal_citation": True,
        }
        tags = ["uk", "parliament", f"{self.house}-library", "research-briefing"]
        tags += [re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") for t in topics if t]
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.COMMENTARY,
            title=title, court=self.issuer, decision_date=when,
            language="en", source_language="en", landing_url=landing,
            raw_bytes=raw, raw_ext=raw_ext, text=text, segments=segments,
            extracted_via=ExtractedVia.STRUCTURED if fmt == "rss" else ExtractedVia.SCRAPE,
            topic_tags=list(dict.fromkeys(tags)), extra=extra,
        )


def _as_date(since: str | None) -> date | None:
    if not since:
        return None
    value = str(since)[:10]
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return parse_pub_date(str(since))
