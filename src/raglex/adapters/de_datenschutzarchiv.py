"""DatenschutzArchiv activity reports: full listing backfill + narrow RSS watch.

The archive gives its detail pages filenames ending in ``.pdf``, but those URLs return
HTML. The actual file is the ``/fileadmin/`` link on that next page. Keeping those two
surfaces distinct prevents storing TYPO3 navigation as though it were a report.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Iterator
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_flag, option_int, resume_floor
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub
from ..extraction.ocr import text_or_ocr

log = logging.getLogger(__name__)

BASE = "https://datenschutzarchiv.org"
LISTING = f"{BASE}/dokumente/taetigkeitsberichte"
RSS = f"{BASE}/rss-feed-aktuelles.xml"
PAGE_SIZE = 10


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _date(value: str | None) -> date | None:
    text = _clean(value)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    return None


def stable_id(url: str) -> str:
    """The detail and RSS surfaces share one identity despite Unicode URL spellings."""
    path = unquote(urlsplit(url).path)
    for marker in ("/detailansicht/", "/fileadmin/"):
        if marker in path:
            path = path.split(marker, 1)[1]
            break
    canonical = quote(path.lstrip("/"), safe="/._-")
    if canonical:
        return f"de/datenschutzarchiv/{canonical}"
    return f"de/datenschutzarchiv/{sha256(url.encode()).hexdigest()[:24]}"


def listing_items(html: bytes | str) -> list[dict]:
    """All result cards, classified by the archive's own document-type field.

    The nominal activity-report listing currently spills into unrelated EDPB opinions
    on its last page. Title heuristics would eventually admit another category; the
    ``dt-doctype`` field is the publisher's explicit classification and is the gate.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for position, card in enumerate(
            soup.select("a.solr-custom-tlp-listpreviewimage__item[href]")):
        href = str(card.get("href") or "").strip()
        heading = card.select_one(".dt-header h3") or card.find("h3")
        kind = card.select_one(".dt-doctype")
        language = card.select_one(".dt-language")
        if not href or heading is None:
            continue
        out.append({
            "url": urljoin(BASE, href),
            "title": _clean(heading.get_text(" ", strip=True)),
            "document_type": _clean(kind.get_text(" ", strip=True) if kind else None),
            "language": _clean(language.get_text(" ", strip=True) if language else None),
            "position": position,
        })
    return out


def is_activity_report(row: dict) -> bool:
    """Reject the category's polluted tail without losing ``TB``/``Jahresbericht`` rows.

    The site itself labels its final EDPB opinions ``Tätigkeitsbericht`` in the card
    metadata, so that field alone is not enough. Genuine rows either say report in the
    title or use the archive's longstanding ``TB_`` filename convention.
    """
    if str(row.get("document_type") or "").casefold() != "tätigkeitsbericht":
        return False
    title = str(row.get("title") or "").casefold()
    filename = unquote(urlsplit(str(row.get("url") or "")).path).rsplit("/", 1)[-1]
    return ("tätigkeitsbericht" in title or "jahresbericht" in title
            or filename.casefold().startswith("tb_")
            or re.search(r"(?:^|\s)\d*[.]?\s*tb(?:\s|$)", title, re.I) is not None)


def last_page(html: bytes | str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pages = [1]
    for link in soup.select(".solr-pagination a[href]"):
        values = parse_qs(urlsplit(str(link.get("href") or "")).query).get(
            "tx_solr[page]", [])
        for value in values:
            try:
                pages.append(int(value))
            except ValueError:
                pass
    return max(pages)


def rss_items(xml: bytes | str) -> list[dict]:
    """Only feed entries whose title contains ``Tätigkeitsbericht`` (case-insensitive)."""
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = ET.fromstring(xml)
    out: list[dict] = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        url = _clean(item.findtext("link"))
        if "tätigkeitsbericht" not in title.casefold() or not url:
            continue
        published = None
        raw_date = _clean(item.findtext("pubDate"))
        if raw_date:
            try:
                published = parsedate_to_datetime(raw_date).date()
            except (TypeError, ValueError):
                pass
        out.append({"title": title, "url": url, "date": published})
    return out


def detail_metadata(html: bytes | str, *, page_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(".tx-dsaextension h1") or soup.find("h1")
    fields: dict[str, str | list[str]] = {}
    for row in soup.select(".tx-dsaextension-fileinfos__item"):
        label = row.select_one(".tx-dsaextension-fileinfos__item__label")
        content = row.select_one(".tx-dsaextension-fileinfos__item__content")
        if label is None or content is None:
            continue
        values = [_clean(node.get_text(" ", strip=True)) for node in content.select("li")]
        fields[_clean(label.get_text(" ", strip=True)).rstrip(":")] = (
            [value for value in values if value]
            if values else _clean(content.get_text(" ", strip=True)))
    download = soup.select_one(".tx-dsaextension-filedownload a[href]")
    pdf_url = urljoin(page_url, str(download.get("href") or "")) if download else None
    return {
        "title": _clean(heading.get_text(" ", strip=True) if heading else None),
        "language": fields.get("Dokumentensprache"),
        "organisations": fields.get("Organisation") or [],
        "revision": fields.get("Revision") or None,
        "date": _date(str(fields.get("Veröffentlichungsdatum") or "")),
        "print_number": fields.get("Drucksachen Nr.") or None,
        "pdf_url": pdf_url,
    }


_GERMAN_ISSUERS = {
    "berlin": "Berlin Commissioner for Data Protection and Freedom of Information",
    "brandenburg": "Brandenburg Commissioner for Data Protection and Access to Information",
    "bremen": "Bremen Commissioner for Data Protection and Freedom of Information",
    "hamburg": "Hamburg Commissioner for Data Protection and Freedom of Information",
    "hessen": "Hesse Commissioner for Data Protection and Freedom of Information",
    "niedersachsen": "Lower Saxony Commissioner for Data Protection",
    "nordrhein-westfalen": "North Rhine-Westphalia Commissioner for Data Protection and Freedom of Information",
    "rheinland-pfalz": "Rhineland-Palatinate Commissioner for Data Protection and Freedom of Information",
    "saarland": "Saarland Independent Data Protection Centre",
    "sachsen": "Saxony Commissioner for Data Protection and Transparency",
    "schleswig-holstein": "Independent Centre for Privacy Protection Schleswig-Holstein",
    "thüringen": "Thuringia Commissioner for Data Protection and Freedom of Information",
    "bfdi": "Federal Commissioner for Data Protection and Freedom of Information (Germany)",
}


def issuer_name(title: str, organisations: list[str]) -> str:
    haystack = f"{title} {' '.join(organisations)}".casefold()
    if "edps" in haystack:
        return "European Data Protection Supervisor (EDPS)"
    if "edpb" in haystack or "edbp" in haystack:
        return "European Data Protection Board (EDPB)"
    if "art. 29" in haystack or "artikel 29" in haystack:
        return "Article 29 Working Party"
    if "österreich" in haystack:
        return "Austrian Data Protection Authority"
    if "liechtenstein" in haystack:
        return "Data Protection Authority of Liechtenstein"
    for token, label in _GERMAN_ISSUERS.items():
        if token in haystack:
            return label
    return organisations[0] if organisations else "German data protection authorities"


def jurisdiction_code(title: str, organisations: list[str]) -> str:
    haystack = f"{title} {' '.join(organisations)}".casefold()
    if any(token in haystack for token in ("edps", "edpb", "edbp", "art. 29", "artikel 29")):
        return "eu"
    if "österreich" in haystack:
        return "at"
    if "liechtenstein" in haystack:
        return "li"
    return "de"


def language_code(label: str | None, url: str) -> str:
    value = _clean(label).casefold()
    if value.startswith("engl") or re.search(r"_en\.pdf$", url, re.I):
        return "en"
    return "de"


class DatenschutzArchivReportsAdapter(BaseAdapter):
    source = "de-datenschutzarchiv-reports"
    min_interval = 0.75

    def __init__(
        self, *, start_offset: int | str | None = None,
        ocr: bool | str | None = None, max_ocr_pages: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.start_offset = resume_floor(option_int(start_offset, 0), PAGE_SIZE)
        self.ocr = option_flag(ocr, True)
        self.max_ocr_pages = max(0, option_int(max_ocr_pages, 300))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if since:
            response = self._client.get(RSS)
            cutoff = _date(str(since)[:10])
            rows = rss_items(response.content)
            for row in rows:
                if cutoff and row["date"] and row["date"] < cutoff:
                    continue
                yield Stub(
                    stable_id=stable_id(row["url"]), landing_url=row["url"],
                    raw_url=row["url"], title=row["title"], hint_date=row["date"],
                    hints={"watermark": row["date"].isoformat() if row["date"] else None,
                           "discovered_via": "rss", "feed_total": len(rows)},
                )
            return

        first = self._client.get(LISTING).content
        end = last_page(first)
        first_page = self.start_offset // PAGE_SIZE + 1
        if max_pages is not None:
            end = min(end, first_page + max(0, max_pages) - 1)
        for page in range(first_page, end + 1):
            body = first if page == 1 else self._client.get(
                LISTING, params={"tx_solr[page]": page}).content
            rows = listing_items(body)
            if not rows:
                raise ValueError(f"DatenschutzArchiv listing page {page} has no result cards")
            for row in rows:
                if not is_activity_report(row):
                    continue
                offset = (page - 1) * PAGE_SIZE + int(row["position"])
                yield Stub(
                    stable_id=stable_id(row["url"]), landing_url=row["url"],
                    raw_url=row["url"], title=row["title"],
                    hints={
                        "language": row["language"], "feed_page": page,
                        "feed_total": end * PAGE_SIZE, "resume_offset": offset,
                        "discovered_via": "listing",
                    },
                )

    def fetch(self, stub: Stub) -> Record | None:
        try:
            detail_response = self._client.get(stub.raw_url)
        except FetchError:
            return None
        meta = detail_metadata(detail_response.content, page_url=str(detail_response.url))
        pdf_url = str(meta.get("pdf_url") or "")
        if not pdf_url or "/fileadmin/" not in urlsplit(pdf_url).path:
            log.warning("%s: detail page has no fileadmin PDF: %s", self.source, stub.raw_url)
            return None
        try:
            pdf = self._client.get(pdf_url).content
        except FetchError:
            return None
        if not pdf.startswith(b"%PDF"):
            log.warning("%s: download is not a PDF: %s", self.source, pdf_url)
            return None
        text, needs_ocr, spans, engine = text_or_ocr(
            pdf, max_pages=self.max_ocr_pages if self.ocr else 0)
        if len((text or "").strip()) < 200:
            needs_ocr = True
        segments = [Segment(label=f"p. {number}", char_start=start, char_end=end,
                            kind="page") for number, start, end in spans]
        organisations = meta.get("organisations") or []
        if isinstance(organisations, str):
            organisations = [organisations]
        title = str(meta.get("title") or stub.title or "Tätigkeitsbericht")
        issuer = issuer_name(title, list(organisations))
        jurisdiction = jurisdiction_code(title, list(organisations))
        language = language_code(str(meta.get("language") or stub.hints.get("language") or ""),
                                 pdf_url)
        return Record(
            source=self.source, stable_id=stub.stable_id,
            doc_type=DocType.PREPARATORY, title=title, court=issuer,
            decision_date=meta.get("date") or stub.hint_date,
            language=language, source_language=language,
            landing_url=stub.landing_url, raw_bytes=pdf, raw_ext="pdf",
            text=text or None, segments=segments, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=["data-protection", "activity-report", "annual-report"],
            extra={
                "jurisdiction": jurisdiction, "issuer": issuer,
                "organisations": list(organisations), "pdf_url": pdf_url,
                "archive_detail_url": stub.landing_url,
                "archive_revision": meta.get("revision"),
                "print_number": meta.get("print_number"),
                "format": engine, "needs_ocr": needs_ocr or None,
                "discovered_via": stub.hints.get("discovered_via"),
                "require_recognized_legal_citation": False,
            },
        )
