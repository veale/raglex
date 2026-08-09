"""The Equality and Human Rights Commission — its guidance, research and codes.

The EHRC is Britain's national equality body and its A-status national human rights
institution: the statutory codes of practice under the **Equality Act 2010**
(``ukpga/2010/15``), the technical guidance on the public sector equality duty, its
section 20 investigations and its advice to Parliament. This adapter is the
Commission's whole published site.

**Discovery is the sitemap, on purpose — the search is a Cloudflare wall.**
``/search?keys=&sort_by=changed`` is the obvious feed and it is the wrong one. It is
behind a Turnstile challenge that a browser has to solve interactively (a plain client
gets a 403 no matter what TLS fingerprint it presents), it pages ten at a time, and its
own pager lies: it prints "3 out of 9" and keeps going past nine — "10 out of 9",
"11 out of 9" — so there is no result count to walk to. ``/sitemap.xml`` is the same
corpus with none of that:

* it is **not** challenged — the same TLS impersonation that gets a 403 on ``/search``
  gets a 200 here, so no browser tier is needed;
* it is one flat ``urlset`` of ~1,976 URLs, not an index to page through; and
* every entry carries ``lastmod``, which is a **real change signal**: an incremental
  run downloads only the pages that actually moved.

Spot-checking a search page against it, every result was present in the sitemap, so
nothing is lost by not fighting the challenge. The content pages themselves are open
to the same impersonating client the sitemap needs.

**"One level deep" is already flat here.** A publication's other formats — the Word
version of a PDF report — live on a separate "alternative formats" page, and that page
is *itself* a sitemap entry with its own downloads panel and a back-link to the report.
Walking the sitemap therefore reaches both without following anything: each is a record,
and the pair is linked by the ``alternative_formats`` / ``found_on`` links kept on them.

Each page's own downloads panel (``PDF, 2.99 MB, 57 pages``; ``DOCX``) is downloaded and
inlined, so a code of practice is searchable as the document and not as its landing page.

The site is a whole organisation's site — careers pages and staff biographies sit in the
sitemap beside the codes of practice — so it opts into RagLex's relevance gate: all of it
is held and deduped, and only what cites a case or an instrument is embedded and searched.

**"The Act" is read from the document, not assumed from the publisher.** A code of
practice binds its own host once — "The Equality Act 2010 (the Act) consolidates…" — and
then prints bare marginal citations (``s.9(1)``, ``s.13(4)``) beside each paragraph.
Without a declared host those carry forward onto whatever statute a passing sentence last
named: in the employment code, 503 of 530 landed on the Employment Rights Act 1996 or the
Civil Partnership Act 2004. Declaring the host fixes 522 of them.

It is tempting to go one better and pin the Equality Act 2010 across the whole site.
That is measurably wrong. On ``/human-rights/`` material the bare provisions belong to the
Human Rights Act 1998 and the Commission's own Equality Act 2006, and pinning the 2010 Act
moves them off both — a briefing on the Convention against Torture acquired eleven false
Equality Act edges. So the host is taken from
:func:`~raglex.citations.declared_instrument_host`, which reads what each document itself
declares, and pages that declare nothing get nothing.
"""

from __future__ import annotations

import html as _html
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from ..citations import declared_instrument_host
from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.errors import FetchError, RateLimitException
from ..core.models import DocType, ExtractedVia, Record, Stub

BASE = "https://www.equalityhumanrights.com"
SITEMAP = BASE + "/sitemap.xml"
#: The Commission's usual subject. Recorded as a tag, never assumed as a document's
#: citation host — see the module docstring.
EQUALITY_ACT_ID = "ukpga/2010/15"

#: Cloudflare answers a real browser handshake; ``/sitemap.xml`` and every content page
#: pass with it. (``/search`` does not — see the module docstring.)
_IMPERSONATE = "firefox135"
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

#: First path segment → the part of the site a page belongs to. Used as the ``section``
#: option's vocabulary and as a topic tag; the EHRC states no content type on the page
#: itself (the "Guidance" pill exists only in the search results it walls off).
SECTIONS = ("guidance", "our-work", "human-rights", "news", "about-us")

#: Paths that are the site's furniture rather than anything it publishes.
_SKIP_PREFIXES = ("/search", "/cy/", "/node/", "/user", "/contact")

_URL_ENTRY = re.compile(r"<url\b.*?</url>", re.S | re.I)
_LOC = re.compile(r"<loc>(.*?)</loc>", re.S | re.I)
_LASTMOD = re.compile(r"<lastmod>(.*?)</lastmod>", re.S | re.I)
_H1 = re.compile(r'<h1[^>]*class="heading"[^>]*>(.*?)</h1>', re.S)
_ANY_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_NODE_ID = re.compile(r'data-history-node-id="(\d+)"')
_HEADER_DATE = re.compile(
    r'class="landing-header__date[^"]*"[^>]*>\s*(Published|Last updated):\s*'
    r"<b>(.*?)</b>", re.S)
#: ``<div  class="document document--pdf">`` — note EHRC's doubled attribute space.
_DOC_BLOCK = re.compile(r'<div\s+class="document document--([a-z0-9]+)"(.*?)</div>\s*</div>',
                        re.S)
_DOC_LINK = re.compile(r'class="link link--large"\s*href="([^"]+)"\s*>(.*?)</a>', re.S)
_DOC_DETAILS = re.compile(r'class="document__details[^"]*">(.*?)</p>', re.S)
_ALT_FORMATS = re.compile(r'href="([^"]+)"\s*>\s*See alternative formats', re.S)
_FOUND_ON = re.compile(r'article__section--found-on.*?<a\s+class="link"\s+href="([^"]+)"',
                       re.S)
_COUNTRY = re.compile(r'countries__list-item--([a-z]+)\b')

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def clean(fragment: str | None) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def parse_date(value: str | None) -> date | None:
    """``20 October 2020`` or an ISO stamp → a date."""
    text = clean(value)
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


class EHRCHTTP:
    """A paced, browser-impersonating GET.

    Cloudflare fronts the whole site. The pages RagLex wants are served to a real
    browser handshake, which ``curl_cffi`` provides without a browser; httpx is kept
    as a fallback so the module imports and tests run wherever curl_cffi is absent.
    """

    #: The edge resets the connection on a repeated large download rather than
    #: answering 429 — a multi-megabyte code of practice fetched back-to-back comes
    #: back as "connection reset by peer", not as a status. A transport failure is
    #: therefore retried with backoff instead of costing the document.
    max_retries = 3

    def __init__(self, source: str, *, min_interval: float = 1.0,
                 timeout: float = 90.0, sleep=time.sleep) -> None:
        self.source = source
        self.min_interval = min_interval
        self.timeout = timeout
        self._sleep = sleep
        self._last_request_at = 0.0
        self._session = None
        self._fallback = None

    def get(self, url: str, **kwargs) -> bytes:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
            self._last_request_at = time.monotonic()
            try:
                status, body = self._request(url, **kwargs)
            except Exception as exc:            # noqa: BLE001 — see max_retries
                last, status, body = exc, None, b""
            if status == 429:
                raise RateLimitException(f"{self.source}: HTTP 429 for {url}")
            if status is not None and status < 400:
                return body
            if status is not None and status < 500:
                raise FetchError(f"{self.source}: HTTP {status} for {url}",
                                 transient=False)
            if status is not None:
                last = FetchError(f"{self.source}: HTTP {status} for {url}",
                                  transient=True)
            self._session = None                # a reset session is not reusable
            self._sleep(2 ** attempt)
        raise FetchError(f"{self.source}: {url} failed after {self.max_retries} "
                         f"attempts ({last})", transient=True)

    def _request(self, url: str, **kwargs) -> tuple[int, bytes]:
        headers = {**_HEADERS, **(kwargs.pop("headers", None) or {})}
        try:
            from curl_cffi import requests as creq

            if self._session is None:
                self._session = creq.Session(impersonate=_IMPERSONATE,
                                             timeout=self.timeout)
            response = self._session.get(url, headers=headers, **kwargs)
            return response.status_code, response.content or b""
        except ImportError:
            import httpx

            if self._fallback is None:
                self._fallback = httpx.Client(
                    timeout=self.timeout, follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 Firefox/135.0", **_HEADERS})
            response = self._fallback.get(url, **kwargs)
            return response.status_code, response.content or b""


@dataclass(frozen=True, slots=True)
class EHRCEntry:
    """One ``<url>`` of the sitemap."""

    url: str
    lastmod: str | None

    @property
    def path(self) -> str:
        return urlsplit(self.url).path or "/"

    @property
    def section(self) -> str | None:
        head = self.path.strip("/").split("/")[0]
        return head if head in SECTIONS else None


@dataclass(frozen=True, slots=True)
class EHRCDocument:
    url: str
    title: str
    ext: str | None
    details: str | None


def parse_sitemap(xml: str) -> list[EHRCEntry]:
    """``sitemap.xml`` → its entries, in file order (pure).

    Parsed per ``<url>`` element rather than by zipping the ``loc`` and ``lastmod``
    lists: the first entry (the home page) has no ``lastmod``, so a positional pairing
    shifts every date on the site by one.
    """
    out: list[EHRCEntry] = []
    for block in _URL_ENTRY.findall(xml or ""):
        loc = _LOC.search(block)
        if not loc:
            continue
        url = _html.unescape(clean(loc.group(1)))
        if not url:
            continue
        when = _LASTMOD.search(block)
        out.append(EHRCEntry(url=url,
                             lastmod=clean(when.group(1)) if when else None))
    return out


def is_content(entry: EHRCEntry) -> bool:
    """Whether a sitemap entry is a page the Commission published, not site furniture."""
    path = entry.path
    if path in ("", "/"):
        return False
    return not any(path.startswith(prefix) for prefix in _SKIP_PREFIXES)


def parse_documents(html: str) -> list[EHRCDocument]:
    """The files in a page's "Document downloads" panel, in page order."""
    out: list[EHRCDocument] = []
    seen: set[str] = set()
    for kind, block in _DOC_BLOCK.findall(html or ""):
        link = _DOC_LINK.search(block)
        if not link:
            continue
        url = urljoin(BASE, _html.unescape(link.group(1)).strip())
        if not url or url in seen:
            continue
        seen.add(url)
        details = _DOC_DETAILS.search(block)
        suffix = urlsplit(url).path.rsplit("/", 1)[-1]
        out.append(EHRCDocument(
            url=url,
            title=clean(link.group(2)),
            ext=(suffix.rsplit(".", 1)[-1].lower() if "." in suffix else kind.lower()),
            details=clean(details.group(1)) if details else None,
        ))
    return out


def parse_dates(html: str) -> tuple[date | None, date | None]:
    """``(published, last updated)`` from the page's own header strip."""
    published = updated = None
    for label, value in _HEADER_DATE.findall(html or ""):
        when = parse_date(value)
        if label.lower() == "published" and published is None:
            published = when
        elif label.lower() == "last updated" and updated is None:
            updated = when
    return published, updated


def stable_id(url: str) -> str:
    """``/guidance/codes-practice/…`` → ``ehrc/guidance/codes-practice/…``."""
    path = urlsplit(url).path.strip("/")
    return "ehrc/" + re.sub(r"[^a-z0-9/_-]+", "-", path.lower()).strip("-/")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


class EHRCAdapter(BaseAdapter):
    source = "uk-ehrc"
    min_interval = 1.0
    requires_js = False          # the TLS handshake clears Cloudflare; no browser
    requires_proxy = False

    def __init__(
        self,
        *,
        section: str | None = None,
        include_documents: bool = True,
        max_documents: int = 20,
        require_recognized_legal_citation: bool = True,
        http: EHRCHTTP | None = None,
    ) -> None:
        section = (section or "").strip().lower() or None
        if section and section not in SECTIONS:
            raise ValueError(f"unknown EHRC section {section!r}; one of {SECTIONS}")
        self.section = section
        self.include_documents = option_flag(include_documents, True)
        self.max_documents = max(0, option_int(max_documents, 20))
        self.require_recognized_legal_citation = option_flag(
            require_recognized_legal_citation, True)
        self._http = http or EHRCHTTP(self.source, min_interval=self.min_interval)

    # ---- discovery -------------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if max_pages is not None and max_pages <= 0:
            return              # the sitemap is one file; only a zero cap suppresses it
        cutoff = (since or "").strip() or None
        entries = [e for e in parse_sitemap(self._http.get(SITEMAP).decode("utf-8", "replace"))
                   if is_content(e)]
        if self.section:
            entries = [e for e in entries if e.section == self.section]
        total = len(entries)
        for entry in entries:
            # ``lastmod`` is the change signal: an unmoved page is not re-fetched, and
            # a page the sitemap dates for the first time is treated as new.
            if cutoff and entry.lastmod and entry.lastmod <= cutoff:
                continue
            yield Stub(
                stable_id=stable_id(entry.url),
                landing_url=entry.url,
                raw_url=entry.url,
                court="Equality and Human Rights Commission",
                hint_date=parse_date(entry.lastmod),
                hints={
                    "lastmod": entry.lastmod,
                    "section": entry.section,
                    "watermark": entry.lastmod,
                    "contenthash": entry.lastmod,
                    "feed_total": total,
                },
            )

    # ---- fetch -----------------------------------------------------------------------

    def _page_text(self, html: str) -> str:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "html.parser")
        main = soup.select_one("main") or soup
        for selector in ("script", "style", "svg", "noscript", "nav", "form",
                         "header", "footer", ".social-share", ".article__print",
                         ".pre-footer", ".banner", ".si__button"):
            for tag in main.select(selector):
                tag.decompose()
        lines = [" ".join(part.split()) for part in main.get_text("\n").splitlines()]
        return "\n".join(line for line in lines if line).strip()

    def _attachment_text(self, doc: EHRCDocument) -> tuple[str, dict]:
        from ..extraction import extract_bytes, text_extension

        meta = {"url": doc.url, "title": doc.title, "format": doc.ext,
                "details": doc.details}
        readable = text_extension(doc.ext)
        if readable is None:
            return "", {**meta, "skipped": "no-text-engine"}
        try:
            body = self._http.get(doc.url)
        except RateLimitException:
            raise
        except Exception:               # noqa: BLE001 — one bad file, not the page
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
        raw = self._http.get(stub.raw_url or stub.landing_url)
        html = raw.decode("utf-8", "replace")
        heading = _H1.search(html) or _ANY_H1.search(html)
        title = clean(heading.group(1)) if heading else stub.title
        published, updated = parse_dates(html)
        countries = sorted(set(_COUNTRY.findall(html)))
        alt = _ALT_FORMATS.search(html)
        found_on = _FOUND_ON.search(html)

        parts = [text for text in (title, self._page_text(html)) if text]
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

        section = stub.hints.get("section")
        node = _NODE_ID.search(html)
        # What THIS document says "the Act" means — see the module docstring for why
        # the Commission's own subject matter is not a safe substitute for it.
        host = declared_instrument_host(text)
        # The page states its own "Last updated"; the sitemap's ``lastmod`` is the
        # publishing system's stamp and stands in when the page prints no date.
        last_updated = updated.isoformat() if updated else stub.hints.get("lastmod")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=title,
            court="Equality and Human Rights Commission",
            decision_date=published or stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="html",
            text=text,
            extracted_via=ExtractedVia.SCRAPE,
            topic_tags=[t for t in dict.fromkeys(
                ["ehrc", "equality", "human-rights",
                 *([slugify(section)] if section else []), *countries]) if t],
            extra={k: v for k, v in {
                "jurisdiction": "gb",
                "issuer": "Equality and Human Rights Commission",
                "section": section,
                "node_id": node.group(1) if node else None,
                "countries": countries,
                "published": published.isoformat() if published else None,
                "updated": last_updated,
                "lastmod": stub.hints.get("lastmod"),
                "contenthash": stub.hints.get("lastmod"),
                "alternative_formats": urljoin(BASE, alt.group(1)) if alt else None,
                "found_on": urljoin(BASE, found_on.group(1)) if found_on else None,
                "attachments": attachments,
                "licence": "© Equality and Human Rights Commission",
                "citation_default_instrument": ({"id": host[0], "kind": host[1]}
                                                if host else None),
                "require_recognized_legal_citation":
                    self.require_recognized_legal_citation,
            }.items() if v not in (None, [], "")},
        )


__all__ = [
    "EQUALITY_ACT_ID",
    "EHRCAdapter",
    "EHRCDocument",
    "EHRCEntry",
    "EHRCHTTP",
    "SECTIONS",
    "is_content",
    "parse_dates",
    "parse_documents",
    "parse_sitemap",
    "stable_id",
]
