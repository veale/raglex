"""IPCO — the Investigatory Powers Commissioner's Office, enumerated from its sitemap.

IPCO has no publications API and its listing pages are filtered views over the same
WordPress posts. The sitemap is the better enumeration anyway, and the reason is
``lastmod``: it is the only place on the site that says when a publication *changed*. An
IPCO annual report that is reissued keeps its URL and its published date, so a listing
crawl cannot see the revision at all — ``wp-sitemap``'s ``lastmod`` can, which makes
"poll this for changes" a real capability here rather than a synonym for "look for new
rows".

The sitemap is small (222 URLs: 141 publications, 81 news) and cheap, so an incremental
run is a single request that is then filtered on ``lastmod`` — the whole archive fits in
one page and there is nothing to page through. It is *not* newest-first, so there is no
cursor to stop at; walking all of it is the correct and complete behaviour, not a
shortcut that happens to work.

**The documents are the PDFs.** An IPCO publication page is a title, a date and one or
more attachments on S3; the prose that matters — the annual report, the correspondence
with a Secretary of State, an IOCCO or OSC inspection report inherited from the bodies
IPCO replaced — is inside the file. So each attachment becomes a document in its own
right, titled by the link text the page gives it, and the older inherited PDFs go through
OCR because several of them are scans of paper.
"""

from __future__ import annotations

import html as _html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Iterator

from ..core.adapter import BaseAdapter, option_flag
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub

log = logging.getLogger("raglex.adapters.uk_ipco")

HOST = "https://www.ipco.org.uk"
SITEMAP = f"{HOST}/post-sitemap.xml"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
                   "Gecko/20100101 Firefox/128.0 (+RagLex legal-research corpus)"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

_SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_H1 = re.compile(r'<h1[^>]*class="[^"]*post-headline[^"]*"[^>]*>(.*?)</h1>', re.S)
_ANY_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_DATE = re.compile(r'class="date"[^>]*>(.*?)</span>', re.S)
_ATTACHMENT = re.compile(
    r'<a[^>]+href="(https?://[^"]+\.pdf)"[^>]*>(.*?)</a>', re.S | re.I)
#: IPCO's own media bucket. A PDF hosted elsewhere on an IPCO page is somebody else's
#: document being linked to, not an IPCO publication.
_OWN_MEDIA = re.compile(r"^https?://(ipco-wpmedia[^/]*|www\.ipco\.org\.uk)/", re.I)


def _clean(fragment: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-") or "document"


def parse_sitemap(raw: bytes) -> list[dict]:
    """``[{"url": …, "lastmod": date | None}]`` in sitemap order."""
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        log.warning("uk-ipco: sitemap did not parse as XML (%d bytes)", len(raw))
        return []
    out: list[dict] = []
    for node in root.iter(f"{_SM_NS}url"):
        loc = (node.findtext(f"{_SM_NS}loc") or "").strip()
        if not loc:
            continue
        out.append({"url": loc, "lastmod": _iso_date(node.findtext(f"{_SM_NS}lastmod"))})
    return out


def _iso_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def parse_page_date(html: str) -> date | None:
    """``Published on <span class="date">16 December 2025</span>``."""
    found = _DATE.search(html or "")
    if not found:
        return None
    text = _clean(found.group(1))
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_attachments(html: str) -> list[dict]:
    """The page's own PDFs, each with the link text that names it.

    A page links the same file two or three times — a thumbnail whose anchor wraps only
    a ``<figure>``, a "Download" button, and the heading that actually names it — so they
    are merged on the URL and the **named** one wins. First-one-wins is wrong here and
    quietly so: the thumbnail comes first in the markup, contributes no text, and every
    attachment ends up anonymous."""
    found: dict[str, str] = {}
    for url, label in _ATTACHMENT.findall(html or ""):
        url = _html.unescape(url)
        if not _OWN_MEDIA.match(url):
            continue
        text = _clean(label)
        if text.lower() in {"download", "download pdf", "pdf", ""}:
            text = ""
        if len(text) > len(found.get(url, "")):
            found[url] = text
        found.setdefault(url, "")
    return [{"url": url, "label": label or None} for url, label in found.items()]


def section_of(url: str) -> str | None:
    """``/publication/annual-report/annual-report-2024/`` → ``annual-report``."""
    parts = [p for p in (url or "").split("ipco.org.uk/")[-1].split("/") if p]
    if len(parts) >= 3 and parts[0] == "publication":
        return parts[1]
    return parts[0] if parts else None


class IPCOPublicationsAdapter(BaseAdapter):
    source = "uk-ipco"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None,
                 include_news: bool = True, sections: str | None = None,
                 ocr: bool = True) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)
        self.include_news = option_flag(include_news, True)
        self.sections = tuple(s.strip().lower() for s in str(sections).split(",")
                              if s.strip()) if sections else ()
        self.ocr = option_flag(ocr, True)

    def _get(self, url: str) -> bytes | None:
        try:
            resp = self._client.get(url, headers=HEADERS)
        except FetchError:
            return None
        content = resp.content or b""
        return content if getattr(resp, "status_code", 200) < 400 and content else None

    # ---- discovery -----------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if max_pages is not None and max_pages <= 0:
            return          # the sitemap is one page; only a zero cap suppresses it
        cutoff = _as_date(since)
        rows = parse_sitemap(self._get(SITEMAP) or b"")
        wanted = [r for r in rows if self._wanted(r["url"])]
        for row in wanted:
            if cutoff and row["lastmod"] and row["lastmod"] < cutoff:
                continue
            url = row["url"]
            yield Stub(
                stable_id=f"uk/ipco/{slugify(url.rstrip('/').rsplit('/', 1)[-1])}",
                landing_url=url, raw_url=url, hint_date=row["lastmod"],
                hints={"lastmod": row["lastmod"].isoformat() if row["lastmod"] else None,
                       "section": section_of(url), "feed_total": len(wanted),
                       "watermark": row["lastmod"].isoformat() if row["lastmod"] else None},
            )

    def _wanted(self, url: str) -> bool:
        section = section_of(url)
        if not self.include_news and (url.split("ipco.org.uk/")[-1].startswith("news/")):
            return False
        return not self.sections or (section or "").lower() in self.sections

    # ---- fetch ---------------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        page = self._get(stub.landing_url or "")
        if not page:
            return None
        html = page.decode("utf-8", "ignore")
        heading = _H1.search(html) or _ANY_H1.search(html)
        title = _clean(heading.group(1)) if heading else None
        published = parse_page_date(html) or stub.hint_date
        attachments = parse_attachments(html)

        text, segments, raw, raw_ext, fmt, needs_ocr = "", [], page, "html", "page", False
        primary = attachments[0]["url"] if attachments else None
        if primary:
            blob = self._get(primary)
            if blob and blob[:5] == b"%PDF-":
                from ..extraction.ocr import text_or_ocr

                text, needs_ocr, spans, fmt = text_or_ocr(
                    blob, max_pages=200 if self.ocr else 0)
                segments = [Segment(label=f"p. {n}", char_start=s, char_end=e, kind="page")
                            for n, s, e in spans]
                raw, raw_ext = blob, "pdf"
        if len(text.strip()) < 200:
            # A news post with no attachment is still a real IPCO statement.
            body = _clean(re.sub(r"(?s)<(script|style|nav|footer|header).*?</\1>", " ",
                                 html))
            if len(body) > len(text):
                text, raw, raw_ext, fmt, segments = body, page, "html", "page", []
        if len(text.strip()) < 200:
            return None

        section = stub.hints.get("section") or section_of(stub.landing_url or "")
        tags = ["uk", "ipco", "investigatory-powers", "oversight"]
        if section:
            tags.append(slugify(section))
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.GUIDANCE,
            title=title or (attachments[0]["label"] if attachments else None),
            court="Investigatory Powers Commissioner's Office",
            decision_date=published, language="en", source_language="en",
            landing_url=stub.landing_url, raw_bytes=raw, raw_ext=raw_ext,
            text=text, segments=segments, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=list(dict.fromkeys(tags)),
            extra={
                "jurisdiction": "uk",
                "issuer": "Investigatory Powers Commissioner's Office",
                "section": section,
                "pdf_url": primary,
                "attachments": attachments,
                "format": fmt,
                "needs_ocr": needs_ocr or None,
                "lastmod": stub.hints.get("lastmod"),
                # Deliberately no ``citation_default_instrument``. IPCO is named for the
                # Investigatory Powers Act 2016, but the register is not about one law:
                # the inherited IOCCO and OSC reports oversee RIPA 2000 and Part III of
                # the Police Act 1997, and a bare "section 22" in one of those means
                # RIPA. A mixed-regime register must not declare a default.
            },
        )


def _as_date(since: str | None) -> date | None:
    if not since:
        return None
    try:
        return datetime.strptime(str(since)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
