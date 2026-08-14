"""Council of Europe publications, ECtHR research publications, treaties, and PACE.

The four sites only share an institution.  They deliberately remain separate adapters:

* ``coe-edoc`` watches the newest-first Edoc list;
* ``coe-edoc-catalog`` backfills every catalogue branch, deduplicating product cards by
  the publisher's reference *before* a detail page or PDF is requested;
* the two ECtHR collections re-read their small indexes because a factsheet is revised at
  the same URL without a date/change feed; and
* ``coe-treaties`` reads the rendered Treaty Office register and the English official PDF;
  and
* ``coe-pace-committees`` walks all committee document/declaration indexes.

The ECtHR, PACE and Treaty Office pages sit behind Cloudflare, and the Treaty Office is a
JavaScript application. Their HTML and protected file downloads therefore use RagLex's
browser tier. A 200 Cloudflare interstitial is never accepted as an empty register or PDF.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date
from typing import Iterator
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    Stub,
    TypedRelation,
)
from ..extraction.ocr import text_or_ocr

log = logging.getLogger("raglex.adapters.council_of_europe")

EDOC = "https://edoc.coe.int"
EDOC_HOME = f"{EDOC}/en/"
EDOC_NEW = f"{EDOC}/en/newproducts?order=epi.date_public.desc"
ECHR = "https://www.echr.coe.int"
TREATIES = "https://www.coe.int/en/web/conventions/full-list"
PACE = "https://pace.coe.int"

PACE_COMMITTEES = {
    "asjur": "Legal Affairs and Human Rights",
    "aspol": "Political Affairs and Democracy",
    # The historic route is still /aspro/, while the committee's references are AS/Rul.
    "aspro": "Rules, Ethics and Immunities",
    "asmon": "Honouring of Obligations and Commitments by Member States",
    "asega": "Equality and Non-Discrimination",
    "ascult": "Culture, Science, Education and Media",
    "asmig": "Migration, Refugees and Displaced Persons",
}

_CATEGORY_PATH = re.compile(r"^/en/(\d+)-[^/]+/?$")
_PRODUCT_ID = re.compile(r"/(\d+)-[^/]+\.html(?:$|[?#])")
_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_TREATY_NUMBER = re.compile(r"(?:treatynum=|(?:C?ETS)(?:\s+No\.?)?\s*)(\d{1,3}[A-Z]?)", re.I)
_DATE_DMY = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
_LANGUAGE_CODES = {
    "english": "en", "french": "fr", "german": "de", "italian": "it",
    "russian": "ru", "spanish": "es", "ukrainian": "uk", "portuguese": "pt",
}


def _clean(node) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def _language(raw: str | None, default: str = "en") -> str:
    value = str(raw or "").strip().casefold()
    if not value:
        return default
    if value in _LANGUAGE_CODES:
        return _LANGUAGE_CODES[value]
    # A multilingual publication should not be silently filed as English merely because
    # its catalogue page uses the English UI.
    for separator in (",", "/", ";", " and "):
        if separator in value:
            return "mul"
    return value[:2]


def _with_page(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(page)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query, doseq=True), parts.fragment))


def parse_edoc_categories(html: str | bytes) -> list[dict]:
    """Every category in the sidebar, parent and child, in publisher order."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("ul.category-top-menu")
    if root is None:
        raise ValueError("Edoc page has no catalogue tree")
    out, seen = [], set()
    for anchor in root.select("a[href]"):
        url = urljoin(EDOC_HOME, str(anchor.get("href")))
        match = _CATEGORY_PATH.match(urlsplit(url).path)
        if not match or url in seen:
            continue
        seen.add(url)
        out.append({"id": match.group(1), "title": _clean(anchor), "url": url.rstrip("/")})
    if not out:
        raise ValueError("Edoc catalogue tree contained no categories")
    return out


def parse_edoc_products(html: str | bytes) -> list[dict]:
    """Read cheap product-card metadata, including the cross-category dedupe key."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for article in soup.select("article.product-miniature"):
        product_id = str(article.get("data-id-product") or "").strip()
        title_link = article.select_one("h2.product-title a[href]")
        reference = re.sub(r"^Ref\.?\s*", "", _clean(article.select_one(".reflist")),
                           flags=re.I).strip()
        if not product_id and title_link:
            match = _PRODUCT_ID.search(str(title_link.get("href")))
            product_id = match.group(1) if match else ""
        if not product_id or not title_link:
            continue
        year_match = _YEAR.search(_clean(title_link.select_one(".datepubli")))
        # Remove the year badge before taking the title; it is metadata, not its name.
        title_copy = BeautifulSoup(str(title_link), "html.parser")
        for badge in title_copy.select(".datepubli"):
            badge.decompose()
        out.append({
            "product_id": product_id,
            "reference": reference or f"product-{product_id}",
            "url": urljoin(EDOC_HOME, str(title_link.get("href"))),
            "title": _clean(title_copy),
            "year": int(year_match.group(1)) if year_match else None,
            "summary": _clean(article.select_one(".minishortdesc")) or None,
        })
    return out


def edoc_last_page(html: str | bytes) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = []
    for anchor in soup.select("nav.pagination a[href]"):
        raw = parse_qs(urlsplit(str(anchor.get("href"))).query).get("page", [])
        if raw and str(raw[0]).isdigit():
            pages.append(int(raw[0]))
        text = _clean(anchor)
        if text.isdigit():
            pages.append(int(text))
    return max(pages, default=1)


def parse_edoc_detail(html: str | bytes) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("h1.prodtitle")
    year_match = _YEAR.search(_clean(title.select_one(".datepubli")) if title else "")
    if title:
        for badge in title.select(".datepubli"):
            badge.decompose()
    features = {}
    for group in soup.select(".product-features .featuregroup"):
        key = _clean(group.select_one(".featurename")).rstrip(" :").casefold()
        value = _clean(group.select_one(".featurevalue"))
        if key and value:
            features[key] = value
    download = soup.select_one(".downloadadd a[href]")
    category = _clean(soup.select_one(".cartouchecat"))
    return {
        "title": _clean(title),
        "year": int(year_match.group(1)) if year_match else None,
        "reference": _clean(soup.select_one(".product-reference [itemprop=sku]")),
        "author": _clean(soup.select_one(".product-manufacturer span")) or None,
        "category": category or None,
        "features": features,
        "summary": _clean(soup.select_one("#description_short .product-description")) or None,
        "contents": _clean(soup.select_one("#description .product-description")) or None,
        "download_url": urljoin(EDOC_HOME, str(download.get("href"))) if download else None,
    }


class CouncilOfEuropeEdocAdapter(BaseAdapter):
    """Council of Europe Publishing PDFs, with distinct watch and catalogue modes."""

    source = "coe-edoc"
    min_interval = 1.0
    PAGE_SIZE = 10

    def __init__(self, *, catalog: object = False, references: str | None = None,
                 max_category_pages: int | str | None = None,
                 start_offset: int | str | None = None,
                 client: RateLimitedClient | None = None) -> None:
        self.catalog = str(catalog).strip().lower() in ("1", "true", "yes", "on")
        if self.catalog:
            self.source = "coe-edoc-catalog"
        self.references = tuple(x.strip() for x in str(references or "").split(",") if x.strip())
        self.max_category_pages = max(1, option_int(max_category_pages, 500))
        self.start_offset = resume_floor(start_offset, self.PAGE_SIZE)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    def _html(self, url: str) -> str:
        response = self._client.get(url)
        body = response.content or b""
        text = body.decode("utf-8", "ignore")
        if not body or "product-miniature" not in text and "category-top-menu" not in text:
            raise FetchError(f"{self.source}: Edoc returned an unreadable page for {url}",
                             transient=True)
        return text

    def _stub(self, row: dict, *, offset: int, category: dict | None = None) -> Stub:
        when = date(int(row["year"]), 1, 1) if row.get("year") else None
        hints = dict(row)
        hints.update({"resume_offset": offset, "category": category})
        if not self.catalog:
            # The product id is the only fine-grained cursor on New items; the cards expose
            # only a year. IDs are slightly disordered, so discovery stops only after a
            # run of old pages rather than at the first old card.
            hints["watermark"] = str(row["product_id"])
        return Stub(stable_id=f"coe/edoc/{row['reference']}", landing_url=row["url"],
                    raw_url=row["url"], title=row["title"], hint_date=when, hints=hints)

    def _targeted(self) -> Iterator[Stub]:
        offset = 0
        for reference in self.references:
            url = f"{EDOC}/en/search?controller=search&s={reference}"
            for row in parse_edoc_products(self._html(url)):
                if row["reference"].casefold() != reference.casefold():
                    continue
                offset += 1
                yield self._stub(row, offset=offset)

    def _new_products(self, since: str | None, max_pages: int | None) -> Iterator[Stub]:
        cutoff = int(since) if str(since or "").isdigit() else None
        page, offset, old_pages = 1, 0, 0
        seen = set()
        while True:
            html = self._html(_with_page(EDOC_NEW, page))
            rows = parse_edoc_products(html)
            if not rows:
                if page == 1:
                    raise FetchError(f"{self.source}: New items returned no products", transient=True)
                return
            page_old = bool(cutoff)
            for row in rows:
                offset += 1
                ref = row["reference"].casefold()
                if ref in seen:
                    continue
                seen.add(ref)
                if cutoff and int(row["product_id"]) <= cutoff:
                    continue
                page_old = False
                if offset > self.start_offset:
                    yield self._stub(row, offset=offset)
            old_pages = old_pages + 1 if page_old else 0
            if old_pages >= 3 or page >= edoc_last_page(html):
                return
            if max_pages is not None and page >= max_pages:
                return
            page += 1

    def _catalog(self, max_pages: int | None) -> Iterator[Stub]:
        home = self._html(EDOC_HOME)
        categories = parse_edoc_categories(home)
        seen, offset, pages_walked = set(), 0, 0
        for category in categories:
            for page in range(1, self.max_category_pages + 1):
                html = self._html(_with_page(category["url"] + "?order=epi.date_public.desc", page))
                rows = parse_edoc_products(html)
                if not rows:
                    break
                for row in rows:
                    # Offset counts the whole feed, including cross-category duplicates.
                    # Resumption must remain on that same scale even when an item emits once.
                    offset += 1
                    ref = row["reference"].casefold()
                    if ref in seen:
                        continue
                    seen.add(ref)
                    if offset > self.start_offset:
                        yield self._stub(row, offset=offset, category=category)
                pages_walked += 1
                if max_pages is not None and pages_walked >= max_pages:
                    return
                if page >= edoc_last_page(html):
                    break

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.references:
            yield from self._targeted()
        elif self.catalog:
            yield from self._catalog(max_pages)
        else:
            yield from self._new_products(since, max_pages)

    def fetch(self, stub: Stub) -> Record | None:
        page = self._html(stub.landing_url or stub.raw_url or "")
        meta = parse_edoc_detail(page)
        reference = meta["reference"] or stub.hints.get("reference")
        pdf_url = meta["download_url"]
        if not reference or not pdf_url:
            raise FetchError(f"{self.source}: product {stub.stable_id} has no reference/PDF",
                             transient=False)
        response = self._client.get(pdf_url, headers={"Referer": stub.landing_url or EDOC_HOME})
        pdf = response.content or b""
        if not pdf.startswith(b"%PDF"):
            raise FetchError(f"{self.source}: download for {reference} was not a PDF",
                             transient=True)
        text, needs_ocr, spans, engine = text_or_ocr(pdf, max_pages=200)
        segments = [Segment(label=f"p. {n}", char_start=start, char_end=end, kind="page")
                    for n, start, end in spans]
        year = meta["year"] or stub.hints.get("year")
        language = _language(meta["features"].get("language"))
        return Record(
            source=self.source, stable_id=f"coe/edoc/{reference}",
            doc_type=DocType.GUIDANCE, title=meta["title"] or stub.title or reference,
            court=meta["author"] or "Council of Europe", language=language,
            source_language=language, decision_date=date(int(year), 1, 1) if year else None,
            landing_url=stub.landing_url, raw_bytes=pdf, raw_ext="pdf", text=text or None,
            segments=segments, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=[x for x in ["council-of-europe", meta["category"]] if x],
            extra={"jurisdiction": "coe", "reference": reference,
                   "aliases": [reference], "author": meta["author"],
                   "category": meta["category"], "catalog_category": stub.hints.get("category"),
                   "summary": meta["summary"] or stub.hints.get("summary"),
                   "table_of_contents": meta["contents"], "features": meta["features"],
                   "pdf_url": pdf_url, "needs_ocr": needs_ocr or None,
                   "extraction_engine": engine})


def parse_echr_publications(html: str | bytes, collection: str) -> list[dict]:
    """English PDF links, deduplicated across the many factsheet topic sections."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for anchor in soup.select("a[href]"):
        url = urljoin(ECHR, str(anchor.get("href")))
        path = urlsplit(url).path
        if "/documents/d/echr/" not in path or not re.search(r"_ENG(?:$|[/?#])", url, re.I):
            continue
        slug = path.rstrip("/").rsplit("/", 1)[-1]
        if slug.casefold() in seen:
            continue
        seen.add(slug.casefold())
        heading = anchor.find_previous(["h2", "h3"])
        nearby = " ".join(anchor.parent.get_text(" ", strip=True).split()) if anchor.parent else ""
        year = _YEAR.search(nearby)
        title = _clean(anchor)
        if not title or title.casefold() == "english":
            previous = anchor.find_previous(["h3", "h4"])
            title = _clean(previous) or slug.replace("_", " ")
        out.append({"slug": slug, "url": url, "title": title,
                    "section": _clean(heading) or None,
                    "year": int(year.group(1)) if year else None,
                    "collection": collection})
    return out


class ECHRPublicationsAdapter(BaseAdapter):
    source = "echr-factsheets"
    min_interval = 2.0
    requires_js = True

    def __init__(self, *, collection: str = "factsheets", documents: str | None = None,
                 fetcher=None, bytes_fetcher=None,
                 start_offset: int | str | None = None) -> None:
        if collection not in ("factsheets", "joint-publications"):
            raise ValueError("collection must be factsheets or joint-publications")
        self.collection = collection
        self.source = f"echr-{collection}"
        self.index_url = f"{ECHR}/{collection}"
        self.documents = tuple(x.strip() for x in str(documents or "").split(",") if x.strip())
        # Accepted because this module contains the Edoc cursor-bearing adapter. These
        # one-page indexes have no cursor of their own, so there is nothing to apply.
        self.start_offset = option_int(start_offset, 0)
        self._fetcher = fetcher
        self._bytes_fetcher = bytes_fetcher

    def _browser(self):
        if self._bytes_fetcher is None:
            from ..scraping.fetcher import get_bytes_fetcher
            self._bytes_fetcher = get_bytes_fetcher()
        return self._bytes_fetcher

    def _html(self) -> str:
        if self._fetcher is not None:
            page = self._fetcher.fetch(self.index_url)
            html = page.html or ""
        else:
            browser = self._browser()
            if not browser.available():
                raise FetchError(f"{self.source}: browser tier is not installed", transient=True)
            html = browser.fetch_html(self.index_url) or ""
        if "just a moment" in html[:6000].casefold() or "/documents/d/echr/" not in html:
            raise FetchError(f"{self.source}: ECHR index was blocked or empty", transient=True)
        return html

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.documents:
            rows = [{"slug": slug, "url": f"{ECHR}/documents/d/echr/{slug}",
                     "title": slug.replace("_", " "), "year": None,
                     "section": None, "collection": self.collection}
                    for slug in self.documents]
        else:
            rows = parse_echr_publications(self._html(), self.collection)
        if not rows:
            raise FetchError(f"{self.source}: no English PDFs found", transient=True)
        for row in rows:
            when = date(row["year"], 1, 1) if row.get("year") else None
            yield Stub(stable_id=f"echr/publication/{self.collection}/{row['slug'].casefold()}",
                       landing_url=self.index_url, raw_url=row["url"], title=row["title"],
                       hint_date=when, hints={**row, "revision": True})

    def fetch(self, stub: Stub) -> Record | None:
        browser = self._browser()
        if not browser.available():
            raise FetchError(f"{self.source}: browser tier is not installed", transient=True)
        pdf = browser.fetch_bytes(stub.raw_url, referer_url=self.index_url) or b""
        if not pdf.startswith(b"%PDF"):
            raise FetchError(f"{self.source}: {stub.raw_url} did not return a PDF", transient=True)
        text, needs_ocr, spans, engine = text_or_ocr(pdf, max_pages=450)
        segments = [Segment(label=f"p. {n}", char_start=start, char_end=end, kind="page")
                    for n, start, end in spans]
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.GUIDANCE,
            title=stub.title, court="European Court of Human Rights", language="en",
            source_language="en", decision_date=stub.hint_date,
            landing_url=self.index_url, raw_bytes=pdf, raw_ext="pdf", text=text or None,
            segments=segments, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=[x for x in ["echr", self.collection,
                                    stub.hints.get("section")] if x],
            extra={"jurisdiction": "coe", "issuer": "European Court of Human Rights",
                   "collection": self.collection, "section": stub.hints.get("section"),
                   "pdf_url": stub.raw_url, "needs_ocr": needs_ocr or None,
                   "extraction_engine": engine,
                   "citation_scope": ["ECLI:CE:ECHR", "ECLI:EU", "ECHR application numbers"]})


def parse_treaty_list(html: str | bytes) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for row in soup.select("tr"):
        raw = " ".join(row.get_text(" ", strip=True).split())
        number = _TREATY_NUMBER.search(raw)
        link = next((a for a in row.select("a[href]")
                     if "treaty-detail" in str(a.get("href"))), None)
        if not number and link:
            number = _TREATY_NUMBER.search(str(link.get("href")))
        if not number:
            continue
        num = number.group(1).upper().zfill(3)
        if num in seen:
            continue
        seen.add(num)
        cells = [_clean(cell) for cell in row.select("th,td")]
        title_cell = cells[1] if len(cells) > 1 else _clean(link)
        title = re.sub(r"\s*\((?:C?ETS)\s+No\.?\s*\d+[A-Z]?\)\s*$", "",
                       title_cell, flags=re.I).strip()
        dates = _DATE_DMY.findall(raw)
        out.append({"number": num, "title": title or f"Council of Europe Treaty {num}",
                    "url": urljoin(TREATIES, str(link.get("href"))) if link else treaty_url(num),
                    "opened": _to_date(dates[0]) if dates else None})
    return out


def treaty_url(number: str) -> str:
    return f"{TREATIES}?module=treaty-detail&treatynum={number}"


def _to_date(parts: tuple[str, str, str]) -> date | None:
    try:
        return date(int(parts[2]), int(parts[1]), int(parts[0]))
    except (ValueError, IndexError):
        return None


def parse_treaty_detail(html: str | bytes, number: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for row in soup.select("tr"):
        cells = row.select("th,td")
        if len(cells) >= 2:
            fields[_clean(cells[0]).rstrip(" :").casefold()] = _clean(cells[1])
    english = None
    for anchor in soup.select("a[href]"):
        if _clean(anchor).casefold() == "english":
            english = urljoin(TREATIES, str(anchor.get("href")))
            break
    title = fields.get("title") or _clean(soup.find(string=re.compile(r"Details of Treaty")))
    reference = fields.get("reference") or f"CETS No. {number}"
    opening = fields.get("opening of the treaty") or ""
    opened = _DATE_DMY.search(opening)
    if not title or not english:
        raise ValueError(f"Treaty {number} detail lacks title or English official text")
    return {"title": title, "reference": reference,
            "short_title": fields.get("short title") or fields.get("abbreviation"),
            "opening": opening,
            "opened": _to_date(opened.groups()) if opened else None,
            "entry_into_force": fields.get("entry in force"),
            "summary": fields.get("summary"), "pdf_url": english}


def treaty_aliases(number: str, detail: dict) -> list[str]:
    """Publisher identifiers and useful official-name variants for one treaty."""
    candidates = [detail.get("reference"), f"CETS {number}", f"CETS No. {number}",
                  f"ETS {number}", f"ETS No. {number}", detail.get("title"),
                  detail.get("short_title")]
    title = str(detail.get("title") or "").strip()
    if title:
        # Detail titles often repeat the series number in parentheses. Both forms occur
        # in prose, so both must name the same node.
        without_series = re.sub(
            r"\s*\((?:C?ETS)(?:\s+No\.?)?\s*\d+[A-Z]?\)\s*$", "", title,
            flags=re.I).strip()
        candidates.append(without_series)
        if without_series.casefold().startswith("council of europe "):
            candidates.append(without_series[len("Council of Europe "):])
    if number == "005":
        candidates += ["European Convention on Human Rights", "Convention for the "
                       "Protection of Human Rights and Fundamental Freedoms", "ECHR"]
    return list(dict.fromkeys(str(value).strip() for value in candidates if value))


_ARTICLE = re.compile(
    r"(?im)^(?:ARTICLE|Article)[ \t]+(\d+[A-Z]?)[ \t]*(?:[-–—:][ \t]*)?.*$"
)
_PARAGRAPH = re.compile(r"(?m)^(\d{1,2})[.)]?\s+(?=\S)")


def treaty_segments(text: str) -> list[Segment]:
    """Article and numbered-paragraph offsets from old and current treaty PDFs."""
    articles = list(_ARTICLE.finditer(text or ""))
    out: list[Segment] = []
    for index, match in enumerate(articles):
        end = articles[index + 1].start() if index + 1 < len(articles) else len(text)
        article = f"Article {match.group(1)}"
        out.append(Segment(label=article, char_start=match.start(), char_end=end,
                           kind="article", level=0))
        body_start = match.end()
        for para in _PARAGRAPH.finditer(text, body_start, end):
            # Page headers and article headings are common bare numbers; paragraphs must
            # begin after the heading and the next non-space character must be prose.
            para_end = end
            following = _PARAGRAPH.search(text, para.end(), end)
            if following:
                para_end = following.start()
            out.append(Segment(label=f"{article}({para.group(1)})",
                               char_start=para.start(), char_end=para_end,
                               kind="paragraph", level=1))
    return out


class CouncilOfEuropeTreatiesAdapter(BaseAdapter):
    source = "coe-treaties"
    min_interval = 2.0
    requires_js = True

    def __init__(self, *, numbers: str | None = None, fetcher=None, bytes_fetcher=None,
                 client: RateLimitedClient | None = None,
                 start_offset: int | str | None = None) -> None:
        self.numbers = tuple(x.strip().upper().zfill(3)
                             for x in str(numbers or "").split(",") if x.strip())
        self._fetcher = fetcher
        self._bytes_fetcher = bytes_fetcher
        # The rendered full list is one response, not a pageable cursor. Accept the
        # shared resume keyword because another adapter in this module reports one.
        self.start_offset = option_int(start_offset, 0)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)

    def _html(self, url: str) -> str:
        if self._fetcher is not None:
            page = self._fetcher.fetch(url)
            html = page.html or ""
        else:
            from ..scraping.fetcher import get_bytes_fetcher
            browser = get_bytes_fetcher()
            if not browser.available():
                raise FetchError(f"{self.source}: browser tier is not installed", transient=True)
            html = browser.fetch_html(url) or ""
        if "just a moment" in html[:6000].casefold() or "enable javascript" in html.casefold():
            raise FetchError(f"{self.source}: Treaty Office was blocked or did not render",
                             transient=True)
        return html

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.numbers:
            rows = [{"number": n, "title": f"Council of Europe Treaty {n}",
                     "url": treaty_url(n), "opened": None} for n in self.numbers]
        else:
            rows = parse_treaty_list(self._html(TREATIES))
        if not rows:
            raise FetchError(f"{self.source}: rendered register contained no treaties",
                             transient=True)
        for row in rows:
            stable_id = "echr/convention" if row["number"] == "005" else f"coe/treaty/{row['number']}"
            yield Stub(stable_id=stable_id, landing_url=row["url"], raw_url=row["url"],
                       title=row["title"], hint_date=row.get("opened"), hints=row)

    def _pdf(self, url: str, referer: str) -> bytes:
        response = self._client.get(url, raise_for_4xx=False,
                                    headers={"Referer": referer})
        if response.status_code < 400 and (response.content or b"").startswith(b"%PDF"):
            return response.content
        if self._bytes_fetcher is None:
            from ..scraping.fetcher import get_bytes_fetcher
            self._bytes_fetcher = get_bytes_fetcher()
        if self._bytes_fetcher.available():
            body = self._bytes_fetcher.fetch_bytes(url, referer_url=referer) or b""
            if body.startswith(b"%PDF"):
                return body
        raise FetchError(f"{self.source}: English official text was not a PDF", transient=True)

    def fetch(self, stub: Stub) -> Record | None:
        number = stub.hints.get("number") or stub.stable_id.rsplit("/", 1)[-1]
        detail = parse_treaty_detail(self._html(stub.landing_url), number)
        pdf = self._pdf(detail["pdf_url"], stub.landing_url)
        text, needs_ocr, spans, engine = text_or_ocr(pdf, max_pages=220)
        structured = treaty_segments(text)
        segments = structured or [Segment(label=f"p. {n}", char_start=start,
                                            char_end=end, kind="page")
                                  for n, start, end in spans]
        aliases = treaty_aliases(number, detail)
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.LEGISLATION,
            title=detail["title"], court="Council of Europe Treaty Office",
            language="en", source_language="en",
            decision_date=detail["opened"] or stub.hint_date,
            landing_url=stub.landing_url, raw_bytes=pdf, raw_ext="pdf", text=text or None,
            segments=segments, extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["council-of-europe", "treaty", detail["reference"]],
            extra={"jurisdiction": "coe", "treaty_number": number,
                   "reference": detail["reference"], "aliases": aliases,
                   "official_title": detail["title"],
                   "short_title": detail["short_title"],
                   "opening": detail["opening"],
                   "entry_into_force": detail["entry_into_force"],
                   "summary": detail["summary"], "pdf_url": detail["pdf_url"],
                   "needs_ocr": needs_ocr or None, "extraction_engine": engine,
                   "structured_articles": sum(s.kind == "article" for s in segments),
                   "structured_paragraphs": sum(s.kind == "paragraph" for s in segments)})


_PACE_REFERENCE = re.compile(
    r"\b(?:AS\s*/\s*(?:Jur|Pol|Rul|Pro|Mon|Ega|Cult|Mig)|PPSD)"
    r"\s*\((?:19|20)\d{2}\)\s*[\w.-]+"
    r"(?:\s+(?:rev(?:ised)?\.?|add(?:endum)?\.?)\s*\d*)*",
    re.I,
)
_HUDOC_ITEMID = re.compile(
    r'''["']?itemid["']?\s*:\s*\[\s*["'](001-\d+)["']''', re.I
)
_HUDOC_QUERY_ID = re.compile(r"(?:[?&#]|\b)i=(001-\d+)(?:\b|$)", re.I)


def _pace_reference_slug(reference: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", reference.casefold()).strip("-")


def _nearby_date(anchor) -> tuple[date | None, str | None]:
    """Nearest preceding date without accidentally borrowing one from navigation."""
    for index, node in enumerate(anchor.previous_elements):
        if index > 35:
            break
        raw = str(node) if isinstance(node, str) else ""
        match = _DATE_DMY.search(raw)
        if match:
            return _to_date(match.groups()), match.group(0)
    return None, None


def parse_pace_committee_documents(html: str | bytes, committee: str) -> list[dict]:
    """Parse one rendered PACE committee archive.

    The archive mixes current ``rm.coe.int`` files with older Assembly URLs.  The
    printed committee reference is therefore the identity; the delivery URL is only a
    rendition and is allowed to change.
    """
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for anchor in soup.select("a[href]"):
        label = _clean(anchor)
        reference_match = _PACE_REFERENCE.search(label)
        if not reference_match:
            continue
        published, date_text = _nearby_date(anchor)
        if published is None:
            # Real archive entries are dated. Requiring the date also rejects PDF links
            # in the page chrome if its markup changes.
            continue
        reference = " ".join(reference_match.group(0).split())
        key = reference.casefold()
        if key in seen:
            continue
        seen.add(key)

        block = None
        for parent in anchor.parents:
            if getattr(parent, "name", None) in ("body", "html"):
                break
            raw = _clean(parent)
            if date_text in raw and len(raw) > len(label) + len(date_text) + 2 \
                    and len(raw) < 2500:
                block = raw
                break
        title = block or label
        title = title.replace(date_text, "", 1).replace(label, "", 1).strip(" –—:-")
        out.append({
            "reference": reference,
            "url": urljoin(PACE, str(anchor.get("href"))),
            "title": title or reference,
            "published": published,
            "committee": committee,
        })
    return out


def pdf_hudoc_links(pdf: bytes) -> list[dict]:
    """Return HUDOC item IDs embedded in PDF link annotations, including link text."""
    try:
        import fitz
        document = fitz.open(stream=pdf, filetype="pdf")
    except Exception:
        # pypdf is in RagLex's base install. It cannot reliably recover the words under
        # an annotation rectangle, but the URI and item ID are the authoritative parts.
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf))
            out, seen = [], set()
            for page_number, page in enumerate(reader.pages, 1):
                for reference in page.get("/Annots") or []:
                    annotation = reference.get_object()
                    action = annotation.get("/A") or {}
                    uri = str(action.get("/URI") or "")
                    if "hudoc.echr.coe.int" not in uri.casefold():
                        continue
                    decoded = unquote(uri)
                    match = _HUDOC_ITEMID.search(decoded) or _HUDOC_QUERY_ID.search(decoded)
                    if not match or match.group(1) in seen:
                        continue
                    itemid = match.group(1)
                    seen.add(itemid)
                    out.append({"itemid": itemid, "url": uri,
                                "text": None, "page": page_number})
            return out
        except Exception:
            log.warning("could not open PACE PDF annotations", exc_info=True)
            return []
    out, seen = [], set()
    try:
        for page_number, page in enumerate(document, 1):
            for link in page.get_links():
                uri = str(link.get("uri") or "")
                if "hudoc.echr.coe.int" not in uri.casefold():
                    continue
                decoded = unquote(uri)
                match = _HUDOC_ITEMID.search(decoded) or _HUDOC_QUERY_ID.search(decoded)
                if not match or match.group(1) in seen:
                    continue
                itemid = match.group(1)
                seen.add(itemid)
                linked_text = ""
                try:
                    linked_text = " ".join(page.get_textbox(link["from"]).split())
                except Exception:
                    pass
                out.append({"itemid": itemid, "url": uri,
                            "text": linked_text or None, "page": page_number})
    finally:
        document.close()
    return out


class PACECommitteeDocumentsAdapter(BaseAdapter):
    """English PACE committee documents and declarations from all listed committees."""

    source = "coe-pace-committees"
    min_interval = 2.0
    requires_js = True
    PAGE_SIZE = 20

    def __init__(self, *, committees: str | None = None, references: str | None = None,
                 fetcher=None, bytes_fetcher=None, client: RateLimitedClient | None = None,
                 start_offset: int | str | None = None) -> None:
        requested = tuple(x.strip().casefold() for x in str(committees or "").split(",")
                          if x.strip())
        unknown = set(requested) - PACE_COMMITTEES.keys()
        if unknown:
            raise ValueError(f"unknown PACE committee codes: {', '.join(sorted(unknown))}")
        self.committees = requested or tuple(PACE_COMMITTEES)
        self.references = {x.strip().casefold() for x in str(references or "").split(",")
                           if x.strip()}
        self.start_offset = resume_floor(start_offset, self.PAGE_SIZE)
        self._fetcher = fetcher
        self._bytes_fetcher = bytes_fetcher
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=180)

    @staticmethod
    def _url(code: str) -> str:
        return f"{PACE}/en/pages/{code}-docdecs"

    def _browser(self):
        if self._bytes_fetcher is None:
            from ..scraping.fetcher import get_bytes_fetcher
            self._bytes_fetcher = get_bytes_fetcher()
        return self._bytes_fetcher

    def _html(self, url: str) -> str:
        if self._fetcher is not None:
            html = self._fetcher.fetch(url).html or ""
        else:
            browser = self._browser()
            if not browser.available():
                raise FetchError(f"{self.source}: browser tier is not installed", transient=True)
            html = browser.fetch_html(url) or ""
        folded = html[:8000].casefold()
        if "just a moment" in folded or "cloudflare" in folded or not _PACE_REFERENCE.search(html):
            raise FetchError(f"{self.source}: PACE committee page was blocked or empty",
                             transient=True)
        return html

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # Fetch all seven small indexes first so duplicates can be merged before even one
        # document download is offered to the pipeline.
        records: dict[str, dict] = {}
        offset = 0
        for code in self.committees:
            page_url = self._url(code)
            rows = parse_pace_committee_documents(self._html(page_url), code)
            if not rows:
                raise FetchError(f"{self.source}: {code} contained no committee documents",
                                 transient=True)
            for row in rows:
                offset += 1
                key = row["reference"].casefold()
                if key in records:
                    if code not in records[key]["committees"]:
                        records[key]["committees"].append(code)
                    continue
                records[key] = {**row, "committees": [code], "resume_offset": offset,
                                "landing_url": page_url}

        for row in records.values():
            if self.references and row["reference"].casefold() not in self.references:
                continue
            if row["resume_offset"] <= self.start_offset:
                continue
            yield Stub(
                stable_id=f"coe/pace/committee/{_pace_reference_slug(row['reference'])}",
                landing_url=row["landing_url"], raw_url=row["url"], title=row["title"],
                hint_date=row["published"], hints=row,
            )

    def _pdf(self, url: str, referer: str) -> bytes:
        response = self._client.get(url, raise_for_4xx=False, headers={"Referer": referer})
        body = response.content or b""
        if response.status_code < 400 and body.startswith(b"%PDF"):
            return body
        browser = self._browser()
        if browser.available():
            body = browser.fetch_bytes(url, referer_url=referer) or b""
            if body.startswith(b"%PDF"):
                return body
        raise FetchError(f"{self.source}: committee document did not return a PDF",
                         transient=True)

    def fetch(self, stub: Stub) -> Record | None:
        pdf = self._pdf(stub.raw_url or "", stub.landing_url or PACE)
        text, needs_ocr, spans, engine = text_or_ocr(pdf, max_pages=350)
        segments = [Segment(label=f"p. {n}", char_start=start, char_end=end, kind="page")
                    for n, start, end in spans]
        hudoc = pdf_hudoc_links(pdf)
        relations = [TypedRelation(
            relationship_type=RelationshipType.MENTIONS,
            raw_citation_string=link["text"] or link["url"],
            # HUDOC's item ID is authoritative even when the visible annotation is only
            # a party name. ECHRAdapter mints this exact key as an alias of its ECLI node.
            dst_id=link["itemid"],
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
            src_anchor=f"p. {link['page']}",
        ) for link in hudoc]
        committees = stub.hints.get("committees") or [stub.hints.get("committee")]
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.PREPARATORY,
            title=stub.title or stub.hints.get("reference"), court="PACE",
            decision_date=stub.hint_date, language="en", source_language="en",
            landing_url=stub.landing_url, raw_bytes=pdf, raw_ext="pdf", text=text or None,
            segments=segments, relations=relations, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=["council-of-europe", "pace", *[c for c in committees if c]],
            extra={"jurisdiction": "coe", "reference": stub.hints.get("reference"),
                   "aliases": [stub.hints.get("reference")],
                   "committees": committees, "pdf_url": stub.raw_url,
                   "hudoc_links": hudoc, "needs_ocr": needs_ocr or None,
                   "extraction_engine": engine},
        )
