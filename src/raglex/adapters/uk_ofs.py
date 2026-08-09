"""The Office for Students' publications — the regulator of English higher education.

The OfS regulates registered higher education providers under the Higher Education and
Research Act 2017 (``ukpga/2017/29``): registration conditions, quality and standards,
access and participation, freedom of speech, and the funding it distributes. Its whole
output is one listing:

    https://www.officeforstudents.org.uk/publications/     (673 items)

**Two indexes, and they see different things.** The publications listing is the feed —
server-rendered, ten per page over 68 pages, ordered by updated date, with each row
carrying its OfS category ("Consultations and their outcomes", "Publications and
letters for providers", "Independent research"…) and its date. The site map at
``/site-map/`` is a *tree*, and it names only 61 publication URLs — but it is the only
place that enumerates the **sub-pages**: a long report is published as a parent page
plus chapters underneath it (``…/gravity-assist…/executive-summary/``,
``…/recommendations/``), and those chapters carry the text. So discovery runs off the
listing, and a publication's own chapters are followed one level deep from the page
itself, which is where they are linked and where the site map merely agrees.

**A publication is a page, a set of chapters, and a downloads panel.** The panel is
``<a class="document pdf|word|excel" href="/media/…">`` with a heading, a
``PDF, 632Kb`` line and a description; a "previous years" accordion often hangs a
decade of superseded editions off the same page. Everything readable — the page prose,
each chapter, each PDF, each DOCX — is extracted and inlined, so a consultation and the
analysis of responses to it are one searchable document. Spreadsheets are recorded but
not read: they have no text engine, and byte-decoding one produces noise, not data.

The ``Ref:`` line ("OfS 2026.38") is the OfS's own citation for a publication and is
kept as an alias, so a reference to it resolves.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.errors import RateLimitException
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)

BASE = "https://www.officeforstudents.org.uk"
LISTING = BASE + "/publications/"
#: The Higher Education and Research Act 2017 — the OfS's constitution and the source
#: of every registration condition it publishes about.
HERA_ID = "ukpga/2017/29"

PAGE_SIZE = 10

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) "
                   "Gecko/20100101 Firefox/153.0"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

_ARTICLE = re.compile(r'<article class="event-listing-article">(.*?)</article>', re.S)
_ROW_LINK = re.compile(r'<a href="(/publications/[^"]+)"[^>]*class="event-listing-article__link"[^>]*>(.*?)</a>',
                       re.S)
_ROW_TEXT = re.compile(r'<div class="event-listing-article__text">(.*?)</div>', re.S)
_ROW_DATE = re.compile(r'<div class="event-listing-article__date">(.*?)</div>', re.S)
_ROW_CATEGORY = re.compile(r'<span class="event-listing-article__category">(.*?)</span>', re.S)
_RESULT_COUNT = re.compile(r"Your search returned\s+([\d,]+)\s+results", re.I)
_LAST_PAGE = re.compile(r'<a href="\?pg=(\d+)">Last</a>')
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_INTRO = re.compile(r'<div class="publication-intro">(.*?)</div>\s*<div class="publication-category',
                    re.S)
_DEF_ITEM = re.compile(r"<(dt|dd)\b[^>]*>(.*?)</\1>", re.S)
#: ``<a class="document pdf" href="/media/…" title="Download …">`` — the downloads
#: panel and the "previous years" tables both use this class, so both are picked up.
_DOCUMENT = re.compile(r'<a[^>]*class="document\s+([a-z]+)"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                       re.S)
_DOC_ATTR = re.compile(r'<a[^>]*href="([^"]+)"[^>]*class="document\s+([a-z]+)"[^>]*>(.*?)</a>',
                       re.S)
_DOC_HEADING = re.compile(r'<div class="document__heading">(.*?)</div>', re.S)
_DOC_INFO = re.compile(r'<div class="document__information">(.*?)</div>', re.S)
_CHILD_LINK = re.compile(r'<a[^>]+href="(/publications/[^"#?]+)"[^>]*>(.*?)</a>', re.S)
_REF = re.compile(r"\bOfS\s*\d{4}\.\d{1,3}\b", re.I)
#: Guide-style publications carry no ``Date:`` row — they print "Published 25 February
#: 2021" in a change-log strip instead, which is the only date they state.
_PUBLISHED = re.compile(r"Published(?:</[a-z]+>)?[:\s]*(?:<[^>]+>\s*)*"
                        r"([0-3]?\d\s+[A-Za-z]{3,9}\s+(?:19|20)\d{2})", re.I)

#: The OfS document classes on a download anchor → the file extension they mean. The
#: class is the icon, not the format, so it is only a fallback for a URL with no suffix.
_CLASS_EXT = {"pdf": "pdf", "word": "docx", "excel": "xlsx", "powerpoint": "pptx",
              "zip": "zip", "csv": "csv"}

#: OfS categories whose items are the regulator deciding something rather than
#: describing it. Everything else is guidance.
_DECISION_CATEGORIES = {"consultations and their outcomes"}


def clean(fragment: str | None) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_date(value: str | None) -> date | None:
    """``05 Aug 2026`` (listing) or ``30 July 2026`` (page) → a date."""
    text = clean(value)
    m = re.search(r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+(?:19|20)\d{2})\b", text)
    if not m:
        return None
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(m.group(1), fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True, slots=True)
class OfSListingRow:
    path: str
    title: str
    summary: str | None
    category: str | None
    date: date | None


@dataclass(frozen=True, slots=True)
class OfSDocument:
    url: str
    title: str
    ext: str | None
    info: str | None


def parse_listing(html: str) -> list[OfSListingRow]:
    """One results page → its publications, in page order (pure)."""
    out: list[OfSListingRow] = []
    for block in _ARTICLE.findall(html or ""):
        link = _ROW_LINK.search(block)
        if not link:
            continue
        summary = _ROW_TEXT.search(block)
        when = _ROW_DATE.search(block)
        category = _ROW_CATEGORY.search(block)
        out.append(OfSListingRow(
            path=link.group(1),
            title=clean(link.group(2)),
            summary=clean(summary.group(1)) if summary else None,
            category=clean(category.group(1)) if category else None,
            date=parse_date(when.group(1) if when else None),
        ))
    return out


def listing_total(html: str) -> int | None:
    """The "Your search returned 673 results" count, when the page states it."""
    m = _RESULT_COUNT.search(html or "")
    return int(m.group(1).replace(",", "")) if m else None


def last_page(html: str) -> int | None:
    """The pager's own "Last" target, so a walk needs no probing to know its length."""
    m = _LAST_PAGE.search(html or "")
    return int(m.group(1)) if m else None


def parse_metadata(html: str) -> dict[str, str]:
    """The ``Ref:``/``Date:`` definition list beside a publication's introduction."""
    out: dict[str, str] = {}
    label: str | None = None
    for kind, body in _DEF_ITEM.findall(html or ""):
        if kind == "dt":
            label = clean(body).rstrip(":").lower() or None
        elif label and label not in out:
            value = clean(body)
            if value:
                out[label] = value
    return out


def parse_documents(html: str) -> list[OfSDocument]:
    """The downloads on a publication page — the panel and the archive tables alike.

    The ``class``/``href`` attribute order differs between the two (the panel writes
    ``class`` first, the accordion tables write ``href`` first), which is why both
    orders are matched rather than one being assumed.
    """
    found: list[tuple[str, str, str]] = [
        (href, kind, body) for kind, href, body in _DOCUMENT.findall(html or "")
    ] + list(_DOC_ATTR.findall(html or ""))
    out: list[OfSDocument] = []
    seen: set[str] = set()
    for href, kind, body in found:
        url = urljoin(BASE, _html.unescape(href).strip())
        if not url or url in seen:
            continue
        seen.add(url)
        suffix = urlsplit(url).path.rsplit("/", 1)[-1]
        ext = (suffix.rsplit(".", 1)[-1].lower() if "." in suffix
               else _CLASS_EXT.get(kind.lower()))
        heading = _DOC_HEADING.search(body)
        info = _DOC_INFO.search(body)
        out.append(OfSDocument(
            url=url,
            title=clean(heading.group(1)) if heading else clean(body),
            ext=ext,
            info=clean(info.group(1)) if info else None,
        ))
    return out


def child_pages(html: str, path: str) -> list[tuple[str, str]]:
    """A publication's own chapters as ``(url path, title)``, in page order.

    A long OfS report is a parent page plus chapters, and the parent's prose is a
    paragraph of introduction — reading only the parent stores the summary and drops
    the report. Only *direct* children are followed, so a chapter's own sub-navigation
    cannot walk the adapter down the tree.

    The title comes from the parent's chapter navigation, not from the chapter's own
    ``h1``: every chapter page repeats the REPORT's title in its ``h1`` and puts the
    chapter's name in an ``h2``, so titling by ``h1`` labels four different chapters
    identically. The first link to a child is the navigation one; later links are prose
    ("read our practical recommendations") and are ignored.
    """
    prefix = "/" + path.strip("/") + "/"
    out: dict[str, str] = {}
    for href, label in _CHILD_LINK.findall(html or ""):
        if not href.startswith(prefix):
            continue
        tail = href[len(prefix):].strip("/")
        if not tail or "/" in tail:
            continue
        url = prefix + tail + "/"
        text = clean(label)
        if url not in out or (not out[url] and text):
            out[url] = text or tail.replace("-", " ").strip().capitalize()
    return list(out.items())


def stable_id(path: str) -> str:
    """``/publications/guide-to-funding/`` → ``ofs/guide-to-funding``."""
    slug = urlsplit(path).path.strip("/")
    slug = slug[len("publications/"):] if slug.startswith("publications/") else slug
    return "ofs/" + re.sub(r"[^a-z0-9/_-]+", "-", slug.lower()).strip("-/")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


class OfSPublicationsAdapter(BaseAdapter):
    source = "uk-ofs"
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        include_documents: bool = True,
        include_child_pages: bool = True,
        max_documents: int = 20,
        require_recognized_legal_citation: bool = False,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.include_documents = option_flag(include_documents, True)
        self.include_child_pages = option_flag(include_child_pages, True)
        self.max_documents = max(0, option_int(max_documents, 20))
        self.require_recognized_legal_citation = option_flag(
            require_recognized_legal_citation, False)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    def _get(self, url: str) -> bytes:
        return self._client.get(url, headers=_HEADERS).content or b""

    # ---- discovery -------------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        cutoff = _as_date(since)
        page = 0
        total: int | None = None
        pages: int | None = None
        while True:
            page += 1
            if max_pages is not None and page > max_pages:
                return
            if pages is not None and page > pages:
                return
            html = self._get(LISTING if page == 1 else f"{LISTING}?pg={page}").decode(
                "utf-8", "replace")
            rows = parse_listing(html)
            if not rows:
                return
            if page == 1:
                total, pages = listing_total(html), last_page(html)
            for row in rows:
                # The listing is ordered by updated date, so the first row at or before
                # the cursor ends the incremental run.
                if cutoff and row.date and row.date < cutoff:
                    return
                yield Stub(
                    stable_id=stable_id(row.path),
                    landing_url=urljoin(BASE, row.path),
                    raw_url=urljoin(BASE, row.path),
                    title=row.title,
                    court="Office for Students",
                    hint_date=row.date,
                    hints={
                        "path": row.path,
                        "category": row.category,
                        "summary": row.summary,
                        "watermark": row.date.isoformat() if row.date else None,
                        # The listing's date IS the updated date (it is what "Sort by:
                        # Updated Date" sorts on), so it is a real change signal: a
                        # publication whose date has not moved is not re-downloaded.
                        "contenthash": row.date.isoformat() if row.date else None,
                        "feed_total": total,
                        "resume_offset": (page - 1) * PAGE_SIZE,
                    },
                )
            if len(rows) < PAGE_SIZE:
                return

    # ---- fetch -----------------------------------------------------------------------

    def _page_text(self, html: str) -> str:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "html.parser")
        main = soup.select_one("#main-content") or soup.select_one("main") or soup
        # The body of a chapter page lives INSIDE ``.umb-block-grid__area-container``,
        # so the promo blocks are removed by naming the sidebar and the widgets — never
        # the grid itself, which once cost every chapter its entire text.
        for selector in ("script", "style", "svg", "noscript", "nav", "form",
                         "header", "footer", ".breadcrumbs", ".improve-experience",
                         ".links-of-interest", ".news-listing--right-col",
                         ".social-share", ".section--social-share"):
            for tag in main.select(selector):
                tag.decompose()
        lines = [" ".join(part.split()) for part in main.get_text("\n").splitlines()]
        return "\n".join(line for line in lines if line).strip()

    def _attachment_text(self, doc: OfSDocument) -> tuple[str, dict]:
        from ..extraction import extract_bytes, text_extension

        meta = {"url": doc.url, "title": doc.title, "format": doc.ext, "info": doc.info}
        readable = text_extension(doc.ext)
        if readable is None:            # a spreadsheet or a zip — held, deliberately unread
            return "", {**meta, "skipped": "no-text-engine"}
        try:
            body = self._get(doc.url)
        except RateLimitException:
            raise
        except Exception:               # noqa: BLE001 — one bad file, not the publication
            return "", {**meta, "skipped": "unavailable"}
        try:
            extracted = extract_bytes(body, ext=readable)
        except Exception:               # noqa: BLE001
            return "", {**meta, "bytes": len(body), "skipped": "extraction-failed"}
        text = (extracted.text or "").strip()
        if not text and readable == "pdf":
            from ..extraction.ocr import text_or_ocr

            text = (text_or_ocr(body)[0] or "").strip()
        return text, {**meta, "bytes": len(body), "text_chars": len(text)}

    def fetch(self, stub: Stub) -> Record | None:
        raw = self._get(stub.raw_url or stub.landing_url)
        html = raw.decode("utf-8", "replace")
        heading = _H1.search(html)
        title = (clean(heading.group(1)) if heading else "") or stub.title
        intro = _INTRO.search(html)
        meta = parse_metadata(html)
        path = stub.hints.get("path") or urlsplit(stub.landing_url or "").path
        category = stub.hints.get("category")

        parts = [text for text in (title, stub.hints.get("summary"),
                                   clean(intro.group(1)) if intro else None,
                                   self._page_text(html)) if text]
        chapters: list[dict] = []
        if self.include_child_pages:
            for child, child_title in child_pages(html, path):
                try:
                    body = self._get(urljoin(BASE, child)).decode("utf-8", "replace")
                except RateLimitException:
                    raise
                except Exception:       # noqa: BLE001
                    continue
                child_text = self._page_text(body)
                if not child_text:
                    continue
                parts.append(f"{child_title}\n{child_text}")
                chapters.append({"url": urljoin(BASE, child), "title": child_title,
                                 "text_chars": len(child_text)})
        attachments: list[dict] = []
        if self.include_documents:
            documents = parse_documents(html)
            if self.max_documents:      # 0 means "however many the page carries"
                documents = documents[: self.max_documents]
            for doc in documents:
                text, row = self._attachment_text(doc)
                attachments.append(row)
                if text:
                    parts.append(f"{doc.title}\n{text}")
        text = "\n\n".join(dict.fromkeys(parts)).strip()
        if len(text) < 40:
            return None

        stated = _PUBLISHED.search(html)
        published = (parse_date(meta.get("date")) or stub.hint_date
                     or parse_date(stated.group(1) if stated else None))
        aliases = list(dict.fromkeys(
            re.sub(r"\s+", " ", ref).upper()
            for ref in _REF.findall(f"{meta.get('ref', '')} {title or ''}")))
        # Everything the OfS publishes is issued under its HERA 2017 powers, so the
        # statute is a structured edge on every record; anything more specific the
        # §5b extractor mines out of the text itself.
        relations = [TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string="Higher Education and Research Act 2017",
            dst_id=HERA_ID,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING)]
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=(DocType.DECISION
                      if (category or "").strip().lower() in _DECISION_CATEGORIES
                      else DocType.GUIDANCE),
            title=title,
            court="Office for Students",
            decision_date=published,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="html",
            text=text,
            relations=relations,
            extracted_via=ExtractedVia.SCRAPE,
            topic_tags=[t for t in dict.fromkeys(
                ["ofs", "higher-education", "regulatory",
                 *([slugify(category)] if category else [])]) if t],
            extra={k: v for k, v in {
                "jurisdiction": "gb",
                "issuer": "Office for Students",
                "category": category,
                "ref": meta.get("ref"),
                "aliases": aliases,
                "summary": stub.hints.get("summary"),
                "published": published.isoformat() if published else None,
                "contenthash": stub.hints.get("contenthash"),
                "chapters": chapters,
                "attachments": attachments,
                "licence": "Crown copyright",
                "citation_default_instrument": {"id": HERA_ID, "kind": "act"},
                "require_recognized_legal_citation":
                    self.require_recognized_legal_citation,
            }.items() if v not in (None, [], "")},
        )


def _as_date(since: str | None) -> date | None:
    if not since:
        return None
    try:
        return datetime.strptime(str(since)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


__all__ = [
    "HERA_ID",
    "OfSDocument",
    "OfSListingRow",
    "OfSPublicationsAdapter",
    "child_pages",
    "last_page",
    "listing_total",
    "parse_documents",
    "parse_listing",
    "parse_metadata",
    "stable_id",
]
