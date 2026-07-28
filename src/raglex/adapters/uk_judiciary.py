"""Judicial guidance published on judiciary.uk — the bench books and the Chief Coroner's
guidance and law sheets.

Three collections, one shape. Each is a landing page on judiciary.uk whose documents are
PDFs behind "Download" buttons (``.related-content__link``), sometimes linked directly and
sometimes one level down on a per-item page:

* **Crown Court Compendium** — Parts I and II, the Judicial College's directions on
  summing up and sentencing, reissued about twice a year;
* **Equal Treatment Bench Book** — one large PDF, revised periodically;
* **Chief Coroner's Guidance, Advice and Law Sheets** — ~30 numbered guidance notes, five
  law sheets and the Treasure guide, most behind their own page.

**Nothing is re-downloaded unless the page changed.** These documents are revised a couple
of times a year but the watch runs monthly, so discovery hashes the landing page's own
document links and compares it with the stored cursor: unchanged page → no stubs → not one
PDF is fetched. A changed page yields its documents, and the pipeline's payload-hash dedup
then skips any individual PDF whose bytes are the same (a page edit that touched one
document doesn't re-store the rest).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..core.segmentation import synthesise_numbered_segments
from ..extraction.extractors import PdfExtractor

BASE = "https://www.judiciary.uk"


@dataclass(frozen=True, slots=True)
class Collection:
    key: str
    label: str
    url: str
    # follow links to per-item pages (the Chief Coroner lists its notes that way) — the
    # bench books link their PDFs straight from the landing page
    follow_items: bool = False
    item_href: str = "/guidance-and-resources/"


COLLECTIONS: tuple[Collection, ...] = (
    Collection("crown-court-compendium", "Crown Court Compendium",
               f"{BASE}/guidance-and-resources/crown-court-compendium/"),
    Collection("equal-treatment-bench-book", "Equal Treatment Bench Book",
               f"{BASE}/about-the-judiciary/diversity/equal-treatment-bench-book/"),
    Collection("chief-coroner", "Chief Coroner's guidance, advice and law sheets",
               f"{BASE}/courts-and-tribunals/coroners-courts/"
               f"coroners-legislation-guidance-and-advice/coroners-guidance/",
               follow_items=True),
)
_BY_KEY = {c.key: c for c in COLLECTIONS}

# "Guidance No 16A: Deprivation of Liberty Safeguards (DoLS)" → the citable reference
_GUIDANCE_NO_RE = re.compile(r"(?i)\bguidance\s+(?:note\s+)?no\.?\s*(\d+[A-Z]?)\b")
_LAW_SHEET_RE = re.compile(r"(?i)\blaw\s+sheet\s+no\.?\s*(\d+[A-Z]?)\b")
# the download button's visible words, which say nothing about the document
_BUTTON_WORDS_RE = re.compile(r"(?i)\b(?:download|file|pdf|opens in a new window)\b")


def _filename_title(filename: str) -> str:
    """"Crown-Court-Compendium-Part-I-Oct-25-Mar-26-update.pdf" → "Crown Court Compendium
    Part I Oct 25 Mar 26 update" — the filename is the only thing distinguishing one
    download from another on these pages."""
    stem = re.sub(r"(?i)\.(pdf|docx?|rtf)$", "", _clean(filename))
    return _clean(re.sub(r"[-_]+", " ", stem)) or "document"


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")[:70]


def parse_documents(html: bytes | str, *, page_url: str) -> list[dict]:
    """Every PDF (or Word attachment) a judiciary.uk page offers, with the label it is
    offered under. Ignores the site chrome: only links inside the main content, and only
    ones that actually point at a document."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main") or soup
    out: list[dict] = []
    seen: set[str] = set()
    for link in main.select("a[href]"):
        href = _clean(str(link.get("href") or ""))
        if not href:
            continue
        url = urljoin(page_url, href)
        path = urlsplit(url).path.lower()
        if not path.endswith((".pdf", ".docx", ".doc", ".rtf")):
            continue
        if url in seen:
            continue
        seen.add(url)
        # The download button reads "Download <hidden filename> file", so the visible text
        # carries no information at all — strip the boilerplate and, when nothing
        # descriptive is left, take the FILENAME, which is what distinguishes Part I from
        # Part II. (Without this every document on a page was titled "file", and the two
        # Compendium parts collided on one id.)
        hidden = link.select_one(".govuk-visually-hidden")
        filename = _clean(hidden.get_text()) if hidden is not None else ""
        label = _clean(link.get_text(" ", strip=True))
        if filename:
            label = _clean(label.replace(filename, " "))
        label = _clean(_BUTTON_WORDS_RE.sub(" ", label))
        if len(label) < 5:
            label = _filename_title(filename or urlsplit(url).path.rsplit("/", 1)[-1])
        out.append({"url": url, "title": label, "filename": filename,
                    "ext": path.rsplit(".", 1)[-1]})
    return out


def parse_item_links(html: bytes | str, *, page_url: str, item_href: str) -> list[dict]:
    """The per-item pages a listing links to (the Chief Coroner's guidance notes and law
    sheets each have their own page, whose download button holds the PDF)."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main") or soup
    out: list[dict] = []
    seen: set[str] = set()
    for link in main.select("a[href]"):
        href = _clean(str(link.get("href") or ""))
        if item_href not in href:
            continue
        url = urljoin(page_url, href)
        if url.rstrip("/") == page_url.rstrip("/") or url in seen:
            continue
        title = _clean(link.get_text(" ", strip=True))
        # a nav/breadcrumb link has no descriptive title; a guidance entry always does
        if len(title) < 12:
            continue
        seen.add(url)
        out.append({"url": url, "title": title})
    return out


def citation_reference(title: str) -> str | None:
    """The form these are cited by: "Guidance No 16A", "Law Sheet No 1"."""
    m = _GUIDANCE_NO_RE.search(title or "")
    if m:
        return f"Guidance No {m.group(1).upper()}"
    m = _LAW_SHEET_RE.search(title or "")
    if m:
        return f"Law Sheet No {m.group(1).upper()}"
    return None


def page_fingerprint(documents: list[dict], items: list[dict]) -> str:
    """A hash of what the landing page OFFERS — the document URLs and the item links, not
    the whole page. The page's markup changes with every unrelated site edit (a nav item,
    an image, a cookie banner); what matters is whether the documents changed."""
    payload = "\n".join(sorted(d["url"] for d in documents)
                        + sorted(i["url"] for i in items))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class JudiciaryGuidanceAdapter(BaseAdapter):
    source = "uk-judiciary"
    min_interval = 2.0

    def __init__(self, *, client: RateLimitedClient | None = None,
                 collection: str | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)
        self.collections = ((_BY_KEY[collection],) if collection in _BY_KEY else COLLECTIONS)

    # -- discover -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        pages = 0
        for coll in self.collections:
            if max_pages is not None and pages >= max_pages:
                return
            pages += 1
            html = self._client.get(coll.url).content
            documents = parse_documents(html, page_url=coll.url)
            items = (parse_item_links(html, page_url=coll.url, item_href=coll.item_href)
                     if coll.follow_items else [])
            fingerprint = page_fingerprint(documents, items)
            # The monthly check: the cursor holds "<collection>:<fingerprint>" per
            # collection. Unchanged → yield nothing, and not one PDF is downloaded.
            if since and f"{coll.key}:{fingerprint}" in since:
                continue
            for doc in documents:
                yield self._stub(coll, doc, landing=coll.url, fingerprint=fingerprint,
                                 alone=len(documents) == 1)
            for item in items:
                item_html = self._client.get(item["url"]).content
                docs = parse_documents(item_html, page_url=item["url"])
                for doc in docs:
                    # the item's own title is the descriptive one ("Guidance No 45: …");
                    # the button label is usually just the filename
                    doc = {**doc, "title": item["title"] or doc["title"]}
                    yield self._stub(coll, doc, landing=item["url"],
                                     fingerprint=fingerprint, alone=len(docs) == 1)

    def _stub(self, coll: Collection, doc: dict, *, landing: str, fingerprint: str,
              alone: bool = True) -> Stub:
        ref = citation_reference(doc["title"])
        key = _slug(ref or doc["title"] or urlsplit(doc["url"]).path.rsplit("/", 1)[-1])
        # A page that offers SEVERAL documents (a guidance note plus its appendix, a
        # current version beside an archived one) would otherwise key them all by the same
        # reference and let the last one win. Qualify each with its own filename.
        if not alone:
            key = f"{key}-{_slug(_filename_title(doc.get('filename') or doc['url']))}"[:90]
        return Stub(
            stable_id=f"uk/judiciary/{coll.key}/{key}",
            landing_url=landing,
            raw_url=doc["url"],
            title=doc["title"],
            court="judiciary",
            hints={"collection": coll.key, "collection_label": coll.label,
                   "reference": ref, "ext": doc["ext"],
                   # the cursor the next run compares against
                   "watermark": f"{coll.key}:{fingerprint}"},
        )

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        try:
            raw = self._client.get(stub.raw_url).content
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        ext = stub.hints.get("ext") or "pdf"
        if ext == "pdf":
            extracted = PdfExtractor().extract(raw, ext="pdf", mime="application/pdf")
            text = (extracted.text or "").strip()
        else:
            from ..extraction import extract_bytes
            text = (extract_bytes(raw, ext=ext).text or "").strip()
        if len(text) < 200:
            return None
        ref = stub.hints.get("reference")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=stub.title or stub.stable_id,
            court="judiciary",
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=ext,
            text=text,
            segments=synthesise_numbered_segments(text),
            extracted_via=ExtractedVia.SCRAPE,
            topic_tags=["uk", "judicial-guidance", stub.hints.get("collection") or ""],
            extra={k: v for k, v in {
                "jurisdiction": "uk",
                "issuer": "Judiciary of England and Wales",
                "collection": stub.hints.get("collection_label"),
                "reference": ref,
                # "Guidance No 45" is how these are cited — mint it as an alias (§5b)
                "aliases": [ref] if ref else None,
                "download_url": stub.raw_url,
            }.items() if v},
        )
