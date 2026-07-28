"""The Commission's digital-strategy library — policy documents and reports (DG CONNECT).

``digital-strategy.ec.europa.eu/en/library`` is the Commission's publication register for
digital policy: AI Act guidelines and Commission opinions, DSA and DMA material, codes of
practice, connectivity and cybersecurity reports. The corpus wants two of its types —
``type=25`` (Policy and legislation) and ``type=28`` (Report / Study) — which the site
filters server-side, so the feed is pre-narrowed to what is worth holding (~175 pages).

Each library ITEM is a news-style page carrying a short editorial summary and a downloads
panel; the substantive material is in the panel, behind
``ec.europa.eu/newsroom/dae/redirection/document/<id>`` links that serve the PDF straight
back (with a nonsense ``Content-Type: /``, hence the magic-byte check). A document per
FILE is stored, so each is separately citable and pinpointable, with the item page as the
landing URL and its summary in the metadata.

**Language variants.** Where a document is published in several languages the panel lists
them as one title per language — "… (English)", "… (French)", … Only the English one is
taken: files are grouped by their title with the language suffix removed, and a group with
more than one member keeps English alone. A title with no language suffix is never touched,
so a document published only in English (the common case) is unaffected.

Discovery is newest-first, so an incremental run stops at the first item at or before the
watermark and a backfill walks to the end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import synthesise_numbered_segments
from ..extraction.extractors import PdfExtractor

BASE = "https://digital-strategy.ec.europa.eu"
# type 25 = Policy and legislation, 28 = Report / Study — the site's own filter
LIBRARY_URL = f"{BASE}/en/library?type=25%7C28"

# The EU's own languages, as the panel spells them in English. A trailing "(English)" is
# what marks a title as one of several language versions of the same document.
_LANGUAGES = (
    "English", "Bulgarian", "Croatian", "Czech", "Danish", "Dutch", "Estonian", "Finnish",
    "French", "German", "Greek", "Hungarian", "Irish", "Italian", "Latvian", "Lithuanian",
    "Maltese", "Polish", "Portuguese", "Romanian", "Slovak", "Slovenian", "Spanish",
    "Swedish", "Norwegian", "Icelandic",
)
_LANG_SUFFIX_RE = re.compile(
    r"\s*[\(\[]\s*(" + "|".join(_LANGUAGES) + r")\s*[\)\]]\s*$", re.IGNORECASE)
# "1 - Guidelines on …" — the panel numbers its files; not part of the title
_LEADING_NUM_RE = re.compile(r"^\s*\d+\s*[-–—.]\s*")
_DATE_RE = re.compile(r"\b(\d{1,2}\s+[A-Za-z]+\s+(?:19|20)\d{2})\b")


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")


def _parse_date(value: str | None) -> date | None:
    m = _DATE_RE.search(_clean(value))
    if not m:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(m.group(1), fmt).date()
        except ValueError:
            continue
    return None


@dataclass(slots=True)
class LibraryItem:
    slug: str
    url: str
    title: str
    kind: str | None = None          # "Policy and legislation" | "Report / Study"
    published: date | None = None
    summary: str | None = None
    files: list[dict] = field(default_factory=list)


def parse_library_page(html: bytes | str) -> list[LibraryItem]:
    """The items on one page of the filtered library listing, in the order shown."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[LibraryItem] = []
    seen: set[str] = set()
    for art in soup.select("article.ecl-content-item"):
        link = art.select_one('.ecl-content-block__title a[href]')
        if link is None:
            continue
        url = urljoin(BASE, _clean(str(link.get("href"))))
        if "/library/" not in url or url in seen:
            continue
        seen.add(url)
        meta = [_clean(li.get_text()) for li
                in art.select(".ecl-content-block__primary-meta-item")]
        desc = art.select_one(".ecl-content-block__description")
        out.append(LibraryItem(
            slug=_slug(urlsplit(url).path.rsplit("/", 1)[-1]),
            url=url,
            title=_clean(link.get_text(" ", strip=True)),
            kind=meta[0] if meta else None,
            published=_parse_date(meta[1]) if len(meta) > 1 else None,
            summary=_clean(desc.get_text(" ", strip=True)) if desc else None,
        ))
    return out


def has_next_page(html: bytes | str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select_one('.ecl-pagination__link[aria-label="Go to next page"]'))


def english_only(files: list[dict]) -> list[dict]:
    """Drop the non-English language versions of a document.

    Files are grouped by their title with a trailing "(Language)" removed. A group with
    more than one member is a set of language versions: English wins, and if English is
    absent nothing is dropped (better to hold the document in some language than none).
    A single file keeps whatever its title says.
    """
    groups: dict[str, list[dict]] = {}
    for f in files:
        stem = _clean(_LANG_SUFFIX_RE.sub("", f["title"]))
        groups.setdefault(stem.casefold(), []).append(f)
    out: list[dict] = []
    for members in groups.values():
        if len(members) == 1:
            out.append(members[0])
            continue
        english = [m for m in members
                   if (_LANG_SUFFIX_RE.search(m["title"]) or [None])
                   and re.search(r"(?i)[\(\[]\s*english\s*[\)\]]\s*$", m["title"])]
        out.extend(english or members)
    # keep the panel's original order
    order = {id(f): i for i, f in enumerate(files)}
    return sorted(out, key=lambda f: order[id(f)])


def parse_item_page(html: bytes | str, item: LibraryItem) -> LibraryItem:
    """Fill an item in from its own page: the downloads panel, and the summary/date when
    the listing didn't carry them."""
    soup = BeautifulSoup(html, "html.parser")
    files: list[dict] = []
    seen: set[str] = set()
    for block in soup.select(".ecl-file"):
        link = block.select_one("a[href]")
        title_el = block.select_one(".ecl-file__title")
        if link is None:
            continue
        url = urljoin(BASE, _clean(str(link.get("href"))))
        if url in seen:
            continue
        seen.add(url)
        title = _clean(title_el.get_text(" ", strip=True)) if title_el else ""
        files.append({"url": url, "title": _clean(_LEADING_NUM_RE.sub("", title)) or item.title})
    item.files = english_only(files)
    if not item.published:
        for li in soup.select(".ecl-page-header__meta-item"):
            item.published = item.published or _parse_date(li.get_text())
    if not item.summary:
        lead = soup.select_one(".ecl-page-header__description, .ecl-paragraph--m")
        item.summary = _clean(lead.get_text(" ", strip=True)) if lead else None
    return item


# "Policy and legislation" is guidance in RagLex's vocabulary; a study is a report, which
# the corpus also files as guidance (see Facade._doc_kind — Guidance/Reports).
_KIND_TAG = {"policy and legislation": "policy", "report / study": "report"}


class DigitalStrategyLibraryAdapter(BaseAdapter):
    source = "eu-digital-strategy"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)

    # -- discover -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        page = 0
        seen: set[str] = set()
        while True:
            if max_pages is not None and page >= max_pages:
                return
            url = LIBRARY_URL if page == 0 else f"{LIBRARY_URL}&page={page}"
            html = self._client.get(url).content
            items = parse_library_page(html)
            if not items:
                return
            for item in items:
                if item.url in seen:
                    continue
                seen.add(item.url)
                wm = item.published.isoformat() if item.published else None
                # newest first: the first item at or before the cursor ends the run
                if since and wm and wm <= since:
                    return
                yield Stub(
                    stable_id=f"eu/digital-strategy/{item.slug}",
                    landing_url=item.url,
                    raw_url=item.url,
                    title=item.title or None,
                    court="European Commission",
                    hint_date=item.published,
                    hints={"watermark": wm, "kind": item.kind, "slug": item.slug,
                           "summary": item.summary},
                )
            if not has_next_page(html):
                return
            page += 1

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        """One library item → the FIRST document in its downloads panel, with the others
        recorded as siblings.

        The pipeline stores one Record per stub, so the panel's remaining files are listed
        in metadata rather than dropped: they are usually annexes to the same instrument,
        and the annex URLs are what a later pass would need to pull them in."""
        try:
            page = self._client.get(stub.landing_url).content
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        item = parse_item_page(page, LibraryItem(
            slug=stub.hints.get("slug") or _slug(stub.stable_id),
            url=stub.landing_url, title=stub.title or "",
            kind=stub.hints.get("kind"), published=stub.hint_date,
            summary=stub.hints.get("summary")))

        text, raw, raw_ext, doc_title = None, None, "html", stub.title
        if item.files:
            first = item.files[0]
            try:
                blob = self._client.get(first["url"]).content
            except FetchError as exc:
                if exc.transient:
                    raise
                blob = None
            # the redirection endpoint answers with Content-Type "/" — trust the bytes
            if blob and blob[:5] == b"%PDF-":
                extracted = PdfExtractor().extract(blob, ext="pdf", mime="application/pdf")
                text, raw, raw_ext = (extracted.text or "").strip(), blob, "pdf"
                doc_title = first["title"] or stub.title
        if not text or len(text) < 200:
            # no readable attachment — keep the item page itself, which carries the
            # Commission's own summary of what was published
            from ..extraction import extract_bytes
            text = (extract_bytes(page, ext="html", mime="text/html").text or "").strip()
            raw, raw_ext = page, "html"
        if len(text) < 200:
            return None

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=doc_title or stub.stable_id,
            court="European Commission",
            decision_date=item.published,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=raw_ext,
            text=text,
            segments=synthesise_numbered_segments(text),
            extracted_via=ExtractedVia.SCRAPE,
            topic_tags=["eu", "digital-policy",
                        _KIND_TAG.get((item.kind or "").casefold(), "publication")],
            extra={k: v for k, v in {
                "jurisdiction": "eu",
                "issuer": "European Commission (DG CONNECT)",
                "library_type": item.kind,
                "summary": item.summary,
                "item_title": stub.title,
                "download_url": item.files[0]["url"] if item.files else None,
                # the annexes published alongside, so a later pass can pull them
                "other_files": [{"title": f["title"], "url": f["url"]}
                                for f in item.files[1:]] or None,
            }.items() if v},
        )
