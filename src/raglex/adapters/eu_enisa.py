"""ENISA's publications register — the EU Agency for Cybersecurity's whole output.

ENISA is where the operative detail of EU cybersecurity law is actually written down.
The NIS2 Directive and Implementing Regulation (EU) 2024/2690 set the requirements at one
level of abstraction; ENISA's technical implementation guidance says what satisfying them
looks like, and the same is true of the CRA's technical documentation, the EUCC scheme
under the Cybersecurity Act, and the threat-landscape reports the Commission cites when
it legislates. None of it is binding and all of it is relied on.

**The list is HTML, the document is one page down.** ``/publications`` is a Drupal view of
cards; the card carries a title, a date and a summary, and the PDF exists only on the
publication's own page (``p.btn-download-file a``). Harvesting the index alone would give
a corpus of abstracts, which is why discovery yields the landing page and ``fetch``
follows it.

**A publication is not always one file.** Some carry annexes, mapping tables and
per-language renditions beside the main PDF. The English PDF is the document; everything
else is recorded as an attachment so the apparatus is visible without being pasted into
the text.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.enisa.europa.eu"
INDEX = f"{BASE}/publications"

#: Cards per page of the Drupal view (measured: 10, with 59 pages back to 2006). Only
#: used to turn a resume cursor back into a page number; the real page size is whatever
#: the server sends.
PAGE_SIZE = 10

# ENISA's own "Publication type" facet, mapped onto the corpus's vocabulary. Everything
# ENISA publishes is guidance in the broad sense, but its annual activity reports and
# work programmes are the agency accounting for itself rather than telling anyone what to
# do, and a reader searching for the agency's POSITION on something should not have to
# wade through them.
_TYPE_DOC_TYPES: tuple[tuple[str, DocType], ...] = (
    ("annual activity report", DocType.PREPARATORY),
    ("work programme", DocType.PREPARATORY),
    ("programming document", DocType.PREPARATORY),
    ("opinion", DocType.OPINION),
    ("guideline", DocType.GUIDANCE),
    ("report", DocType.GUIDANCE),
)


def doc_type_for(publication_type: str | None, title: str | None = None) -> DocType:
    haystack = f"{publication_type or ''} {title or ''}".casefold()
    for needle, kind in _TYPE_DOC_TYPES:
        if needle in haystack:
            return kind
    return DocType.GUIDANCE


def _clean(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node is not None else ""


def _slug(url: str) -> str:
    """``/publications/nis2-technical-implementation-guidance`` → the slug.

    ENISA emits its own hrefs with a TRAILING SPACE inside the quotes
    (``href="/publications/foo "``). Left in, the space survives into the stable_id and
    into every request, and the same publication is then harvested again under the
    stripped id the day the template is fixed.
    """
    path = urlsplit(url.strip()).path.rstrip("/")
    return path.removeprefix("/publications/").strip("/ ") or "publication"


def _iso_date(value: str | None) -> date | None:
    text = " ".join((value or "").split())
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%d %B, %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:30], fmt).date()
        except ValueError:
            continue
    return None


def index_rows(html: bytes | str) -> list[dict]:
    """One dict per card on a page of the publications index."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.select(".publications-item"):
        link = card.select_one(".publication-content h3 a[href]") or card.select_one("h3 a[href]")
        if not link:
            continue
        stamp = card.select_one("time[datetime]")
        out.append({
            "url": urljoin(BASE, str(link["href"]).strip().split("#", 1)[0]),
            "title": _clean(link),
            "date": _iso_date(str(stamp["datetime"]) if stamp and stamp.get("datetime") else None),
            "summary": _clean(card.select_one(".publication-content .content")),
        })
    return out


def last_page(html: bytes | str) -> int | None:
    """The highest ``?page=`` the pager offers (0-based, so 58 means 59 pages)."""
    soup = BeautifulSoup(html, "html.parser")
    best: int | None = None
    for link in soup.select('a[href*="page="]'):
        query = dict(parse_qsl(urlsplit(str(link.get("href") or "")).query))
        try:
            page = int(query.get("page", ""))
        except ValueError:
            continue
        best = page if best is None else max(best, page)
    return best


#: Files worth pulling text out of. ENISA attaches .xlsx mapping tables and .docx
#: templates beside its PDFs; those are recorded, not read.
_READABLE = (".pdf",)


def parse_publication(html: bytes | str, page_url: str) -> dict:
    """A publication's own page: title, date, description, taxonomy and its files."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("article.node--type-publications") or soup.select_one("main") or soup
    heading = soup.select_one("h1 .field--name-title") or soup.find("h1")

    meta: dict[str, str] = {}
    for item in root.select(".publication-metadata-detail > li"):
        label = item.select_one(".label-detail")
        if not label:
            continue
        key = _clean(label).rstrip(":").casefold()
        # The label sits INSIDE the value's parent, so it has to come back out of the
        # text or every value reads "Publication type ENISA Reports".
        value = _clean(item)
        meta[key] = value[len(_clean(label)):].strip() or value

    topics = [_clean(a) for a in root.select("li.related-topics a")]
    audience = [_clean(a) for a in root.select('.publication-metadata-detail a[href*="/audience/"]')]
    description = _clean(root.select_one(".field--name-field-description"))
    body = _clean(root.select_one(".field--name-body"))

    files: list[dict] = []
    seen: set[str] = set()
    # Order matters: the download button is the publication; the language list and the
    # body's "Additional materials" links are annexes. The first readable file becomes
    # the document, so the button must be looked at first.
    for selector in ("p.btn-download-file a[href]",
                     ".publication-metadata-detail li.lang a[href]",
                     ".field--name-body a[href]",
                     ".publication-image a[href]"):
        for link in root.select(selector):
            url = urljoin(page_url, str(link["href"]).strip().split("#", 1)[0])
            path = urlsplit(url).path.casefold()
            if url in seen or not path.startswith("/sites/default/files/"):
                continue
            seen.add(url)
            files.append({"url": url, "title": _clean(link) or None,
                          "readable": path.endswith(_READABLE)})
    return {
        "title": _clean(heading) or None,
        "date": _iso_date(meta.get("publication date")
                          or _clean(root.select_one("p.publish-date .date"))),
        "description": description,
        "body": body,
        "publication_type": meta.get("publication type"),
        "topics": topics,
        "audience": audience,
        "files": files,
    }


class ENISAPublicationsAdapter(BaseAdapter):
    source = "eu-enisa"
    min_interval = 1.0

    def __init__(self, *, client: RateLimitedClient | None = None,
                 start_offset: int | str | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)
        # §1: the stubs report resume_offset, so the constructor MUST take one back, and
        # resume_floor backs off a page — re-reading twelve cards the pipeline already
        # holds costs one request; resuming one card late loses that publication for good.
        self.start_offset = resume_floor(option_int(start_offset, 0), PAGE_SIZE)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Walk the index newest-first, stopping at the cursor.

        The view is one series ordered by publication date, so a ``since`` early-stop is
        sound: everything below an already-seen row is older. (The featured block above
        the list is NOT ``.publications-item``, so it cannot end the walk on its first
        row.) A backfill walks all 59 pages; keep-current normally reads one.
        """
        offset = self.start_offset
        page = offset // PAGE_SIZE
        end: int | None = None
        pages_read = 0
        seen: set[str] = set()
        while max_pages is None or pages_read < max_pages:
            try:
                response = self._client.get(INDEX, params={"page": page} if page else None)
            except FetchError:
                return
            if end is None:
                end = last_page(response.content)
            rows = index_rows(response.content)
            if not rows:
                # An empty page is either the end of the walk or ENISA serving a
                # non-page: past the pager's own last page it is the end, before it the
                # register has broken and must not be reported as exhausted.
                if end is not None and page <= end:
                    raise FetchError(
                        f"{self.source}: page {page} of {end} listed no publications",
                        transient=True)
                return
            # Drupal answers an OUT-OF-RANGE ?page= with page 0 — a 200 carrying ten
            # perfectly valid cards. Without this the walk past the last page loops on
            # the newest ten for ever, and the pager (which bounds it below) is a
            # template detail that could vanish in a redesign.
            fresh = [r for r in rows if f"eu/enisa/{_slug(r['url'])}" not in seen]
            if not fresh:
                return
            for index, row in enumerate(rows):
                published = row.get("date")
                if since and published and published.isoformat() < since[:10]:
                    return                      # newest-first: everything below is older
                seen.add(f"eu/enisa/{_slug(row['url'])}")
                yield Stub(
                    stable_id=f"eu/enisa/{_slug(row['url'])}",
                    landing_url=row["url"], raw_url=row["url"],
                    title=row["title"], court="ENISA",
                    hint_date=published,
                    hints={
                        "summary": row.get("summary"),
                        # Counts the WHOLE feed, not the position within this page, so a
                        # resume lands where the walk actually stopped.
                        "resume_offset": page * PAGE_SIZE + index,
                        "watermark": published.isoformat() if published else None,
                    },
                )
            pages_read += 1
            page += 1
            if end is not None and page > end:
                return

    def fetch(self, stub: Stub) -> Record | None:
        try:
            response = self._client.get(stub.raw_url)
        except FetchError:
            return None
        parsed = parse_publication(response.content, str(response.url))

        # The page's own prose first — for the handful of publications ENISA posts with
        # no file at all, it is the whole record rather than nothing.
        parts = [p for p in (parsed.get("description"), parsed.get("body")) if p]
        raw, ext = response.content, "html"
        attachments: list[dict] = []
        for item in parsed.get("files") or []:
            if not item.get("readable"):
                attachments.append({"url": item["url"], "title": item.get("title"),
                                    "bytes": None, "text_chars": 0})
                continue
            try:
                blob = self._client.get(item["url"]).content
                extracted = extract_bytes(blob, ext="pdf", mime="application/pdf")
            except (FetchError, ValueError):
                # Recorded with no text rather than dropped: a publication whose PDF is
                # temporarily 404 must still show that the PDF is what it consists of.
                attachments.append({"url": item["url"], "title": item.get("title"),
                                    "bytes": None, "text_chars": 0})
                continue
            text = (extracted.text or "").strip()
            if text:
                parts.append(text)
                if ext != "pdf":
                    raw, ext = blob, "pdf"      # the first readable PDF is the document
            attachments.append({"url": item["url"], "title": item.get("title"),
                                "bytes": len(blob), "text_chars": len(text)})
        text = "\n\n".join(parts).strip()
        if len(text) < 120:
            return None

        title = parsed.get("title") or stub.title
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=doc_type_for(parsed.get("publication_type"), title),
            title=title,
            court="ENISA",
            decision_date=parsed.get("date") or stub.hint_date,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=ext, text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=list(dict.fromkeys(
                ["enisa", "cybersecurity", *(parsed.get("topics") or [])])),
            extra={
                "jurisdiction": "eu",
                "publication_type": parsed.get("publication_type"),
                "topics": parsed.get("topics") or [],
                "audience": parsed.get("audience") or [],
                "summary": parsed.get("description") or stub.hints.get("summary"),
                "attachments": attachments,
                # ENISA guidance names its instruments in prose ("the NIS2 Directive",
                # "the CRA") and almost never by CELEX or OJ reference, so requiring a
                # recognised legal citation before a document is kept would discard the
                # whole register.
                "require_recognized_legal_citation": False,
            },
        )
