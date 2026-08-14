"""Belgian regulator publications: Market Court judgments and BIPT material.

Two publishers expose materially different registers.  The GBA/APD page links directly
to Market Court scans.  BIPT uses a paged register, then a publication page, and its
``file_reference`` results add one more topic page before the publication.  Keeping that
shape here prevents a tempting but incomplete scrape of listing-page links only.

Court PDFs are deliberately re-OCR'd in Dutch and French.  Many contain a small native
cover-page text layer over otherwise scanned pages; ordinary "OCR only if empty" logic
would therefore archive just the cover.  Administrative decisions and opinions retain
their born-digital text and use the normal OCR fallback only when it is actually needed.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction.ocr import ocr_pdf, text_or_ocr
from .be_gba_decisions import detect_language, parse_dutch_date, result_total

GBA_BASE = "https://www.gegevensbeschermingsautoriteit.be"
GBA_SEARCH = f"{GBA_BASE}/burger/zoeken"
GBA_QUERY = {
    "q": "", "search_category[]": "taxonomy:publications",
    "search_type[]": "decision",
    "search_subtype[]": "taxonomy:dispute_chamber_judgements_market_court",
    "s": "recent", "l": 50,
}
BIPT_BASE = "https://www.bipt.be"
BIPT_SEARCH = f"{BIPT_BASE}/operators/search"
PAGE_SIZE_GBA = 50
PAGE_SIZE_BIPT = 10

_ECLI = re.compile(r"\bECLI\s*:\s*BE\s*:\s*[A-Z0-9]+\s*:\s*\d{4}\s*:\s*[A-Z0-9.]+", re.I)
_DOCKET = re.compile(r"\b((?:19|20)\d{2})\s*/\s*AR\s*/\s*(\d+(?:\s*-\s*(?:AR\s*/\s*)?\d+)*)", re.I)
_GBA_AR = re.compile(r"\bAR\s*/\s*(\d+(?:\s*-\s*(?:AR\s*/\s*)?\d+)*)", re.I)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b")
_EN_MONTHS = {m.casefold(): i for i, m in enumerate(
    ("", "January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"))}


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def _normalise_ecli(text: str) -> str | None:
    match = _ECLI.search(text[:30000])
    return re.sub(r"\s+", "", match.group(0)).upper() if match else None


def _english_date(value: str | None) -> date | None:
    match = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+((?:19|20)\d{2})\b", value or "")
    if not match:
        return None
    month = _EN_MONTHS.get(match.group(2).casefold())
    try:
        return date(int(match.group(3)), int(month or 0), int(match.group(1)))
    except ValueError:
        return None


def _numeric_date(value: str | None) -> date | None:
    match = _NUMERIC_DATE.search(value or "")
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError:
        return None


def _language_from_link(link) -> str | None:
    text = _clean(link.get_text(" ", strip=True)).casefold()
    name = str(link.get("href") or "").rsplit("/", 1)[-1].casefold()
    if any(x in text for x in ("décision", "avis ", "arrêt", "jugement")) or name.startswith(("decision-", "avis-", "arret-", "jugement-")):
        return "fr"
    if any(x in text for x in ("besluit", "advies", "arrest", "vonnis")) or name.startswith(("besluit-", "advies-", "arrest-", "vonnis-")):
        return "nl"
    return None


def market_court_stubs(html: bytes | str) -> list[Stub]:
    """Parse GBA cards, keying same-docket interim/final rulings by their own date."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Stub] = []
    seen: set[str] = set()
    for card in soup.select(".media"):
        link = card.select_one(".media-title a[href]")
        if not link:
            continue
        href = str(link.get("href") or "").split("#", 1)[0]
        if not href.lower().endswith(".pdf"):
            continue
        title = _clean(link.get_text(" ", strip=True))
        summary_node = card.select_one(".media-description")
        summary = _clean(summary_node.get_text(" ", strip=True) if summary_node else "")
        decided = parse_dutch_date(title)
        docket_match = _DOCKET.search(f"{title} {summary}")
        if docket_match:
            docket = f"{docket_match.group(1)}/AR/{re.sub(r'\s+', '', docket_match.group(2))}"
        else:
            ar = _GBA_AR.search(f"{title} {summary}")
            docket = f"AR/{re.sub(r'\s+', '', ar.group(1))}" if ar else ""
        identity = f"{decided.isoformat() if decided else 'undated'}-{_slug(docket or title)}"
        sid = f"be/market-court/gba/{identity}"
        if sid in seen:
            continue
        seen.add(sid)
        url = urljoin(GBA_BASE, href)
        out.append(Stub(
            stable_id=sid, landing_url=url, raw_url=url, hint_date=decided,
            title=title, court="be-market-court",
            hints={"docket": docket or None, "summary": summary or None,
                   "judgment_kind": "interim" if title.casefold().startswith("tussenarrest") else "judgment",
                   "language": "fr" if "frans" in title.casefold() else None,
                   "watermark": decided.isoformat() if decided else None},
        ))
    return out


def bipt_total(html: bytes | str) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    match = re.search(r"([\d.,\s]+)\s+results?\s+found", _clean(soup.title.get_text(" ") if soup.title else ""), re.I)
    if not match:
        match = re.search(r"([\d.,\s]+)\s+results?\s+found", _clean(soup.get_text(" ")), re.I)
    digits = re.sub(r"\D", "", match.group(1)) if match else ""
    return int(digits) if digits else None


def bipt_listing_links(html: bytes | str, *, decisions_only: bool = False) -> list[tuple[str, str, date | None]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("li.list-group-item"):
        link = card.select_one("a[href]")
        heading = card.select_one("h2")
        if not link or not heading:
            continue
        title = _clean(heading.get_text(" ", strip=True))
        if decisions_only and not title.casefold().startswith(("decision", "décision", "besluit")):
            continue
        time = card.select_one("time[datetime]")
        published = None
        if time:
            try:
                published = date.fromisoformat(str(time.get("datetime") or "")[:10])
            except ValueError:
                pass
        out.append((urljoin(BIPT_BASE, str(link.get("href") or "")), title, published))
    return out


def bipt_topic_decisions(html: bytes | str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for link in soup.select('a[href*="/operators/publication/"]'):
        title = _clean(link.get_text(" ", strip=True))
        if title.casefold().startswith(("decision", "décision", "besluit")):
            pair = (urljoin(BIPT_BASE, str(link.get("href") or "")), title)
            if pair not in out:
                out.append(pair)
    return out


def bipt_publication(html: bytes | str, landing_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean((soup.select_one("h1") or soup.title).get_text(" ", strip=True) if (soup.select_one("h1") or soup.title) else "")
    title = re.sub(r"\s*\|\s*BIPT$", "", title)
    meta = _clean(" ".join(x.get_text(" ", strip=True) for x in soup.select(".file-meta")))
    date_match = re.search(r"\bDate\s+(\d{1,2}/\d{1,2}/\d{4})", meta, re.I)
    pub_match = re.search(r"\bPublication date\s+(\d{1,2}/\d{1,2}/\d{4})", meta, re.I)
    files = []
    for link in soup.select('a[href*="/file/"]'):
        href = urljoin(BIPT_BASE, str(link.get("href") or ""))
        lang = _language_from_link(link)
        if lang in ("fr", "nl") and (href, lang) not in files:
            files.append((href, lang))
    return {"title": title, "decision_date": _numeric_date(date_match.group(1) if date_match else None),
            "publication_date": _numeric_date(pub_match.group(1) if pub_match else None),
            "files": files, "landing_url": landing_url}


def classify_bipt_court(title: str, text: str = "") -> tuple[str, str]:
    """Return stable court slug + human typology from title, then OCR text fallback."""
    value = f"{title}\n{text[:6000]}".casefold()
    if "court of cassation" in value or "cour de cassation" in value or "hof van cassatie" in value:
        return "be-cassation", "Court of Cassation"
    if "court of justice of the european union" in value or "cour de justice de l'union" in value:
        return "cjeu", "Court of Justice of the European Union"
    if "council of state" in value or "conseil d'état" in value or "raad van state" in value:
        return "be-rvsce", "Council of State"
    if "court of first instance" in value or "tribunal de première instance" in value or "rechtbank van eerste aanleg" in value:
        return "be-brussels-first-instance", "Brussels Court of First Instance"
    if any(x in value for x in ("market court", "cour des marchés", "marktenhof")):
        return "be-market-court", "Market Court (Brussels Court of Appeal)"
    return "be-brussels-court-of-appeal", "Brussels Court of Appeal"


def _forced_bilingual_ocr(blob: bytes) -> tuple[str, bool, list, str]:
    """Always OCR court scans; 300dpi makes lightly skewed regulator copies readable."""
    text = (ocr_pdf(blob, dpi=300, max_pages=160, language="nld+fra") or "").strip()
    if text:
        return text, False, [], "tesseract-300dpi-nld+fra"
    fallback, needs, spans, engine = text_or_ocr(blob, max_pages=160)
    return fallback, True if fallback else needs, spans, engine


class GBAMarketCourtAdapter(BaseAdapter):
    source = "be-market-court-gba"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None, start_offset: int | str | None = None):
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval, timeout=180)
        self.start_offset = resume_floor(option_int(start_offset, 0), PAGE_SIZE_GBA)

    def _page(self, page: int):
        return self._client.get(GBA_SEARCH, params={**GBA_QUERY, "p": page}, headers={
            "Accept": "text/html,application/xhtml+xml", "Accept-Language": "nl-BE,nl;q=0.9,fr;q=0.8"})

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        start = self.start_offset // PAGE_SIZE_GBA
        first = self._page(start).content
        total = result_total(first)
        if total is None:
            raise FetchError(f"{self.source}: register returned no result total")
        last = max(0, (total - 1) // PAGE_SIZE_GBA)
        if max_pages is not None:
            last = min(last, start + max_pages - 1)
        for page in range(start, last + 1):
            html = first if page == start else self._page(page).content
            rows = market_court_stubs(html)
            if not rows:
                raise FetchError(f"{self.source}: page {page} was empty inside a {total}-result register")
            for stub in rows:
                stub.hints["feed_total"] = total
                stub.hints["resume_offset"] = page * PAGE_SIZE_GBA
                yield stub

    def fetch(self, stub: Stub) -> Record | None:
        blob = self._client.get(stub.raw_url).content
        if not blob.startswith(b"%PDF"):
            raise FetchError(f"{self.source}: advertised judgment was not a PDF")
        text, needs, spans, engine = _forced_bilingual_ocr(blob)
        if len(text) < 200:
            return None
        language = str(stub.hints.get("language") or detect_language(text))
        ecli = _normalise_ecli(text)
        docket_match = _DOCKET.search(text[:20000])
        docket = (f"{docket_match.group(1)}/AR/{re.sub(r'\s+', '', docket_match.group(2))}"
                  if docket_match else stub.hints.get("docket"))
        fallback = stub.stable_id
        return Record(
            source=self.source, stable_id=ecli or fallback, doc_type=DocType.JUDGMENT,
            title=stub.title, court="be-market-court", decision_date=stub.hint_date,
            language=language, source_language=language, ecli=ecli,
            landing_url=stub.landing_url, raw_bytes=blob, raw_ext="pdf", text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["belgium", "market-court", "data-protection", f"lang-{language}"],
            extra={"jurisdiction": "be", "publisher": "GBA/APD", "docket_number": docket,
                   "citation_number": docket, "judgment_kind": stub.hints.get("judgment_kind"),
                   "summary": stub.hints.get("summary"), "aliases": [fallback] + ([docket] if docket else []),
                   "needs_ocr": needs, "page_spans": spans, "extraction_engine": engine,
                   "citation_languages": ["nl", "fr"]},
        )


class BIPTPublicationsAdapter(BaseAdapter):
    """BIPT publication PDFs. ``kind`` selects judgments, decisions, or opinions."""
    min_interval = 1.0

    def __init__(self, *, kind: str, source: str, client: RateLimitedClient | None = None,
                 start_offset: int | str | None = None):
        if kind not in ("judgments", "decisions", "opinions"):
            raise ValueError("kind must be judgments, decisions, or opinions")
        self.kind, self.source = kind, source
        self._client = client or RateLimitedClient(source, min_interval=self.min_interval, timeout=180)
        self.start_offset = resume_floor(option_int(start_offset, 0), PAGE_SIZE_BIPT)

    def _queries(self):
        if self.kind == "judgments":
            return [("publication_type:dispute", False)]
        if self.kind == "opinions":
            return [("publication_type:opinion", False)]
        return [("publication_type:decision", False), ("file_reference", True)]

    def _search(self, facet: str, page: int):
        params = {"tgGroup": "operators", "s": "publication_date", "fdate[start]": "",
                  "fdate[end]": "", "q": "", "p": page}
        if facet == "file_reference":
            params["type[]"] = "file_reference"
        else:
            params["type[]"] = facet
        return self._client.get(BIPT_SEARCH, params=params).content

    def _publication_urls(self, url: str, title: str) -> list[tuple[str, str]]:
        if "/operators/topic/" not in url:
            return [(url, title)]
        return bipt_topic_decisions(self._client.get(url).content)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        global_page = 0
        emitted_pages = 0
        seen_publications: set[str] = set()
        for facet, decisions_only in self._queries():
            first = self._search(facet, 0)
            total = bipt_total(first)
            if total is None:
                raise FetchError(f"{self.source}: {facet} register returned no result total")
            pages = max(1, (total + PAGE_SIZE_BIPT - 1) // PAGE_SIZE_BIPT)
            for page in range(pages):
                if global_page < self.start_offset // PAGE_SIZE_BIPT:
                    global_page += 1
                    continue
                if max_pages is not None and emitted_pages >= max_pages:
                    return
                html = first if page == 0 else self._search(facet, page)
                links = bipt_listing_links(html, decisions_only=decisions_only)
                if not links and page * PAGE_SIZE_BIPT < total:
                    raise FetchError(f"{self.source}: {facet} page {page} was unexpectedly empty")
                for url, listing_title, listed_date in links:
                    for landing, child_title in self._publication_urls(url, listing_title):
                        if landing in seen_publications:
                            continue
                        seen_publications.add(landing)
                        publication = bipt_publication(self._client.get(landing).content, landing)
                        if self.kind == "judgments" and not re.search(r"\b(?:judg(?:e)?ment|arr[eê]t|arrest|vonnis)\b", publication["title"], re.I):
                            continue
                        for raw_url, language in publication["files"]:
                            base_slug = urlsplit(landing).path.rstrip("/").rsplit("/", 1)[-1]
                            sid = f"be/bipt/{self.kind}/{base_slug}/{language}"
                            yield Stub(
                                stable_id=sid, landing_url=landing, raw_url=raw_url,
                                title=publication["title"] or child_title,
                                hint_date=publication["decision_date"] or _english_date(child_title),
                                court="be-brussels-court-of-appeal" if self.kind == "judgments" else "bipt",
                                hints={"language": language, "publication_date":
                                       (publication["publication_date"] or listed_date).isoformat()
                                       if (publication["publication_date"] or listed_date) else None,
                                       "resume_offset": global_page * PAGE_SIZE_BIPT,
                                       "feed_total": total, "parent_id": f"be/bipt/{self.kind}/{base_slug}"},
                            )
                global_page += 1
                emitted_pages += 1

    def fetch(self, stub: Stub) -> Record | None:
        blob = self._client.get(stub.raw_url).content
        if not blob.startswith(b"%PDF"):
            raise FetchError(f"{self.source}: advertised publication was not a PDF")
        if self.kind == "judgments":
            text, needs, spans, engine = _forced_bilingual_ocr(blob)
        else:
            text, needs, spans, engine = text_or_ocr(blob, max_pages=160)
        if len(text) < 120:
            return None
        language = str(stub.hints.get("language") or detect_language(text))
        ecli = _normalise_ecli(text) if self.kind == "judgments" else None
        docket_match = _DOCKET.search(text[:30000]) if self.kind == "judgments" else None
        docket = (f"{docket_match.group(1)}/AR/{re.sub(r'\s+', '', docket_match.group(2))}"
                  if docket_match else None)
        court, court_type = classify_bipt_court(stub.title or "", text) if self.kind == "judgments" else ("bipt", "BIPT")
        doc_type = {"judgments": DocType.JUDGMENT, "decisions": DocType.DECISION,
                    "opinions": DocType.OPINION}[self.kind]
        fallback = stub.stable_id
        stable_id = f"{ecli}/{language}" if ecli else fallback
        return Record(
            source=self.source, stable_id=stable_id, doc_type=doc_type,
            title=stub.title, court=court, decision_date=stub.hint_date,
            language=language, source_language=language, ecli=ecli,
            landing_url=stub.landing_url, raw_bytes=blob, raw_ext="pdf", text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["belgium", "bipt", self.kind, f"lang-{language}"],
            extra={"jurisdiction": "be", "publisher": "BIPT", "publication_type": self.kind,
                   "publication_date": stub.hints.get("publication_date"), "court_type": court_type,
                   "docket_number": docket, "citation_number": docket,
                   "translation_group": stub.hints.get("parent_id"),
                   "aliases": [fallback] + ([docket] if docket else []), "needs_ocr": needs,
                   "page_spans": spans, "extraction_engine": engine,
                   "citation_languages": ["nl", "fr"]},
        )


class BIPTJudgmentsAdapter(BIPTPublicationsAdapter):
    def __init__(self, **kwargs):
        super().__init__(kind="judgments", source="be-bipt-judgments", **kwargs)


class BIPTDecisionsAdapter(BIPTPublicationsAdapter):
    def __init__(self, **kwargs):
        super().__init__(kind="decisions", source="be-bipt-decisions", **kwargs)


class BIPTOpinionsAdapter(BIPTPublicationsAdapter):
    def __init__(self, **kwargs):
        super().__init__(kind="opinions", source="be-bipt-opinions", **kwargs)
