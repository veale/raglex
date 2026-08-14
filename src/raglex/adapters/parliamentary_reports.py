"""French and Dutch parliamentary reports.

These registers all expose useful listing metadata but put the actual report one hop
deeper.  The adapters deliberately store the complete rendition, never the notice or
summary: one-page HTML is preferred, then a whole-report PDF.  Tweede Kamer publishes
the authoritative body as DOCX rather than HTML/PDF, so its OData ``Resource`` stream is
used directly.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urljoin
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter, option_int
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes, text_or_ocr

SENAT = "https://www.senat.fr"
SENAT_REPORTS = f"{SENAT}/rapports/rapports-information.html"
SENAT_FEED = f"{SENAT}/rss/rapports.xml"
SENAT_LC = f"{SENAT}/lc/"
ASSEMBLEE_LIST = "https://www2.assemblee-nationale.fr/documents/liste"
ASSEMBLEE = "https://www.assemblee-nationale.fr"
TK_ODATA = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0"

_FR_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    "decembre": 12,
}

# Debate/committee reports and standalone research/audit reports.  Legislative bill
# reports (``Verslag (initiatief)wetsvoorstel``) are intentionally absent.
DEFAULT_TK_TYPES = (
    "Verslag van een algemeen overleg",
    "Verslag van een schriftelijk overleg",
    "Verslag van een commissiedebat",
    "Verslag van een wetgevingsoverleg",
    "Verslag van een notaoverleg",
    "Verslag van een bijeenkomst",
    "Verslag van een hoorzitting / rondetafelgesprek",
    "Verslag commissie Verzoekschriften en de Burgerinitiatieven",
    "Verslag van een werkbezoek",
    "Verslag van een rapporteur",
    "Verslag van een politieke dialoog",
    "Rapport",
    "Rapport Algemene Rekenkamer",
    "Jaarverslag",
    "Wetenschappelijke factsheet",
    "Wetenschapstoets",
)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _fr_date(value: str | None) -> date | None:
    match = re.search(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})", value or "", re.I)
    if not match:
        return None
    month = _FR_MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


def _iso_date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _content_html(raw: bytes) -> tuple[bytes, str]:
    """Strip site chrome while preserving the report's HTML as the raw payload."""
    soup = BeautifulSoup(raw, "html.parser")
    root = soup.select_one("main") or soup.body or soup
    for node in root.select("script, style, nav, header, footer, aside, .breadcrumb"):
        node.decompose()
    body = str(root).encode("utf-8")
    return body, extract_bytes(body, ext="html", mime="text/html").text.strip()


def _whole_report_link(raw: bytes, base_url: str) -> str | None:
    """Find complete HTML first, then a report PDF (never a synthesis)."""
    soup = BeautifulSoup(raw, "html.parser")
    links = [(urljoin(base_url, str(a.get("href") or "")), _clean(a.get_text(" ")))
             for a in soup.select("a[href]")]
    for url, label in links:
        if url.lower().endswith(('_mono.html', '_mono.htm')) or (
                "une seule page" in label.casefold() and "html" in label.casefold()):
            return url
    for url, label in links:
        low = url.lower()
        if low.endswith(".pdf") and "syn.pdf" not in low and "synth" not in label.casefold():
            return url
    return None


def _senat_id(url: str) -> str | None:
    match = re.search(r"/(?:rap|lc)/([^/]+)/", url)
    if not match:
        match = re.search(r"/([a-z]+\d+(?:-\d+)?)-notice\.html", url, re.I)
    return f"fr/senat/{match.group(1).lower()}" if match else None


def parse_senat_listing(raw: bytes, base_url: str = SENAT_REPORTS) -> list[Stub]:
    soup = BeautifulSoup(raw, "html.parser")
    out: list[Stub] = []
    for link in soup.select('a[href*="/notice-rapport/"][href*="-notice.html"]'):
        url = urljoin(base_url, str(link.get("href")))
        stable = _senat_id(url)
        if not stable or "/lc" in stable:
            continue
        row = link.find_parent("li")
        outer = row.find_parent("li") if row else None
        title_node = outer.find("strong") if outer else None
        title = _clean(title_node.get_text(" ") if title_node else link.get_text(" "))
        text = _clean(outer.get_text(" ") if outer else (row.get_text(" ") if row else ""))
        out.append(Stub(stable_id=stable, landing_url=url, raw_url=url,
                        title=title, hint_date=_fr_date(text)))
    return list({stub.stable_id: stub for stub in out}.values())


def parse_senat_atom(raw: bytes) -> list[Stub]:
    # The live feed declares ISO-8859-15. ElementTree honours that declaration.
    root = ElementTree.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out: list[Stub] = []
    for entry in root.findall("a:entry", ns):
        link = entry.find("a:link", ns)
        url = (link.get("href") if link is not None else "") or ""
        # Information/control reports are sometimes linked through their notice and
        # sometimes straight to ``/rap/rYY-N``; ``/rap/lYY-N`` are bill reports.
        if not (re.search(r"/rap/r\d{2}-", url)
                or re.search(r"/notice-rapport/\d{4}/r\d{2}-", url)):
            continue
        stable = _senat_id(url)
        if not stable:
            continue
        title = _clean(entry.findtext("a:title", default="", namespaces=ns))
        published = entry.findtext("a:published", default="", namespaces=ns)
        out.append(Stub(stable_id=stable, landing_url=url.replace("http://", "https://"),
                        raw_url=url.replace("http://", "https://"), title=title,
                        hint_date=_iso_date(published)))
    return out


class SenatInformationReportsAdapter(BaseAdapter):
    source = "fr-senat-reports"
    min_interval = 0.35

    def __init__(self, *, client: RateLimitedClient | None = None,
                 start_offset: int = 0) -> None:
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)
        self.start_offset = max(0, int(start_offset or 0))

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if since:
            stubs = parse_senat_atom(self._client.get(SENAT_FEED).content)
            for index, stub in enumerate(stubs):
                if stub.hint_date and stub.hint_date.isoformat() < since[:10]:
                    break
                stub.hints["resume_offset"] = index
                yield stub
            return

        index_raw = self._client.get(SENAT_REPORTS).content
        index_soup = BeautifulSoup(index_raw, "html.parser")
        pages = [SENAT_REPORTS] + [urljoin(SENAT_REPORTS, str(a.get("href")))
            for a in index_soup.select('a[href*="rapports-information-"]')]
        pages = list(dict.fromkeys(pages))
        emitted = 0
        for page_no, page in enumerate(pages):
            if max_pages is not None and page_no >= max_pages:
                return
            raw = index_raw if page_no == 0 else self._client.get(page).content
            for stub in parse_senat_listing(raw, page):
                if emitted < self.start_offset:
                    emitted += 1
                    continue
                stub.hints["resume_offset"] = emitted
                emitted += 1
                yield stub

    def fetch(self, stub: Stub) -> Record | None:
        page_url = stub.raw_url or stub.landing_url or ""
        page = self._client.get(page_url).content
        soup = BeautifulSoup(page, "html.parser")
        canonical = soup.select_one('link[rel="canonical"]')
        if canonical and canonical.get("href"):
            page_url = urljoin(page_url, str(canonical.get("href")))
            page = self._client.get(page_url).content
        target = _whole_report_link(page, page_url)
        if not target:
            return None
        return _french_report_record(self, stub, target)


def parse_lc_listing(raw: bytes, base_url: str = SENAT_LC) -> list[Stub]:
    soup = BeautifulSoup(raw, "html.parser")
    out: list[Stub] = []
    for link in soup.select('a[href*="/notice-rapport/"][href*="/lc"][href*="-notice.html"]'):
        url = urljoin(base_url, str(link.get("href")))
        match = re.search(r"/(lc\d+)-notice\.html", url, re.I)
        text = _clean(link.get_text(" "))
        # Explanatory notes under a study link back to an older LC paper. Those are
        # cross-references, not another row; the primary row always begins ``LC N :``.
        if not match or not re.match(r"^LC\s*\d+\s*:", text, re.I):
            continue
        year = None
        accordion = link.find_parent(class_="accordion-item")
        heading = accordion.select_one(".accordion-button") if accordion else None
        if heading and _clean(heading.get_text()).isdigit():
            year = int(_clean(heading.get_text()))
        month = next((number for name, number in _FR_MONTHS.items()
                      if re.search(rf"\b{name}\b", text, re.I)), 1)
        out.append(Stub(
            stable_id=f"fr/senat/{match.group(1).lower()}", landing_url=url, raw_url=url,
            title=re.sub(r"^LC\s*\d+\s*:\s*", "", text, flags=re.I).rsplit("(", 1)[0].strip(),
            hint_date=date(year, month, 1) if year else None,
        ))
    return out


class SenatComparativeLawAdapter(SenatInformationReportsAdapter):
    source = "fr-senat-lc"

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if max_pages == 0:
            return
        for index, stub in enumerate(parse_lc_listing(self._client.get(SENAT_LC).content)):
            if index < self.start_offset:
                continue
            stub.hints["resume_offset"] = index
            yield stub


def _french_report_record(adapter: BaseAdapter, stub: Stub, target: str) -> Record | None:
    response = adapter._client.get(target)  # type: ignore[attr-defined]
    blob = response.content
    content_type = (response.headers.get("content-type") or "").lower()
    is_pdf = blob.startswith(b"%PDF") or "application/pdf" in content_type
    if is_pdf:
        text, needs_ocr, page_spans, engine = text_or_ocr(blob)
        raw, ext = blob, "pdf"
    else:
        raw, text = _content_html(blob)
        needs_ocr, page_spans, engine, ext = False, None, "html-strip", "html"
    if len((text or "").strip()) < 200:
        return None
    return Record(
        source=adapter.source, stable_id=stub.stable_id, doc_type=DocType.PREPARATORY,
        title=stub.title, court="French Senate", decision_date=stub.hint_date,
        language="fr", source_language="fr", landing_url=stub.landing_url,
        raw_bytes=raw, raw_ext=ext, text=text.strip(), extracted_via=ExtractedVia.SCRAPE,
        extra={"document_url": target, "needs_ocr": needs_ocr,
               "page_spans": page_spans, "extraction_engine": engine},
    )


def parse_assemblee_listing(raw: bytes, legislature: int, *, offset: int = 0) -> list[Stub]:
    soup = BeautifulSoup(raw, "html.parser")
    out: list[Stub] = []
    rows = soup.select("ul.liens-liste > li[data-id]")
    for index, row in enumerate(rows):
        data_id = str(row.get("data-id") or "").strip()
        link = next((a for a in row.select("a[href]")
                     if "Document" in a.get_text(" ") or "/rap-info/" in str(a.get("href"))), None)
        if not data_id or link is None:
            continue
        url = urljoin(ASSEMBLEE, str(link.get("href")))
        title = _clean((row.find("h3") or row).get_text(" "))
        when = _clean((row.select_one(".heure") or row).get_text(" "))
        out.append(Stub(
            stable_id=f"fr/an/{data_id.removeprefix('OMC_').lower()}",
            landing_url=url, raw_url=url, title=title, hint_date=_fr_date(when),
            hints={"legislature": legislature, "resume_offset": offset + index},
        ))
    return out


class AssembleeInformationReportsAdapter(BaseAdapter):
    source = "fr-an-reports"
    min_interval = 0.35

    def __init__(self, *, client: RateLimitedClient | None = None,
                 legislatures: str | None = None, start_offset: int = 0,
                 page_size=None) -> None:
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)
        raw = str(legislatures or "").strip()
        self.legislatures = tuple(int(v) for v in raw.split(",") if v.strip().isdigit()) \
            if raw else tuple(range(17, 0, -1))
        self.start_offset = max(0, int(start_offset or 0))
        self.page_size = max(1, min(option_int(page_size, 150), 150))

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        pages = 0
        absolute = 0
        for legislature in self.legislatures:
            offset = 0
            while True:
                if max_pages is not None and pages >= max_pages:
                    return
                response = self._client.get(ASSEMBLEE_LIST, params={
                    "offset": offset, "limit": self.page_size,
                    "type": "rapports-information", "legis": legislature,
                    "no_margin": "false", "nbresultats": 0,
                })
                rows = parse_assemblee_listing(response.content, legislature, offset=absolute)
                pages += 1
                if not rows:
                    break
                stop_legislature = False
                for stub in rows:
                    absolute += 1
                    if absolute <= self.start_offset:
                        continue
                    if since and stub.hint_date and stub.hint_date.isoformat() < since[:10]:
                        stop_legislature = True
                        break
                    yield stub
                if stop_legislature or len(rows) < self.page_size:
                    break
                offset += len(rows)
            # A watch only needs the current legislature. Older legislatures are closed.
            if since:
                return

    def fetch(self, stub: Stub) -> Record | None:
        landing = stub.raw_url or stub.landing_url or ""
        page = self._client.get(landing).content
        soup = BeautifulSoup(page, "html.parser")
        html_link = next((urljoin(landing, str(a.get("href"))) for a in soup.select("a[href]")
                          if "/dyn/opendata/" in str(a.get("href"))
                          and str(a.get("href")).lower().endswith(".html")), None)
        pdf_link = next((urljoin(landing, str(a.get("href"))) for a in soup.select("a[href]")
                         if str(a.get("href")).lower().endswith(".pdf")), None)
        target = html_link or pdf_link
        if not target:
            return None
        record = _french_report_record(self, stub, target)
        if record:
            record.court = "French National Assembly"
            record.extra["legislature"] = stub.hints.get("legislature")
        return record


def _tk_filter(types: tuple[str, ...]) -> str:
    escaped = [value.replace("'", "''") for value in types]
    return "Verwijderd eq false and (" + " or ".join(f"Soort eq '{v}'" for v in escaped) + ")"


def _tk_stub(item: dict, *, offset: int = 0, total: int | None = None) -> Stub | None:
    uuid = str(item.get("Id") or "").strip()
    number = str(item.get("DocumentNummer") or "").strip()
    if not uuid:
        return None
    stable = f"nl/tk/{number.casefold()}" if number else f"nl/tk/{uuid}"
    landing = (f"https://www.tweedekamer.nl/kamerstukken/detail?id={number}&did={number}"
               if number else None)
    changed = str(item.get("GewijzigdOp") or item.get("Datum") or "")
    hints = {"uuid": uuid, "document_number": number, "kind": item.get("Soort"),
             "content_type": item.get("ContentType"), "watermark": changed,
             # OData exposes no byte checksum, but GewijzigdOp is its authoritative
             # revision signal. Persist it as contenthash so a changed held document is
             # re-fetched while overlap rows with the same stamp remain cheap dedupes.
             "contenthash": changed,
             "resume_offset": offset}
    if total:
        hints["feed_total"] = total
    return Stub(stable_id=stable, landing_url=landing,
                raw_url=f"{TK_ODATA}/Document({uuid})/Resource",
                title=_clean(item.get("Titel") or item.get("Onderwerp")),
                hint_date=_iso_date(item.get("Datum")), hints=hints)


class TweedeKamerReportsAdapter(BaseAdapter):
    source = "nl-tk-reports"
    min_interval = 0.15

    def __init__(self, *, client: RateLimitedClient | None = None,
                 types: str | None = None, start_offset: int = 0, page_size=None) -> None:
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)
        self.types = tuple(v.strip() for v in str(types).split(",") if v.strip()) \
            if types else DEFAULT_TK_TYPES
        self.start_offset = max(0, int(start_offset or 0))
        self.page_size = max(1, min(option_int(page_size, 250), 250))

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        skip = self.start_offset
        pages = 0
        while True:
            if max_pages is not None and pages >= max_pages:
                return
            filters = _tk_filter(self.types)
            if since:
                filters += f" and GewijzigdOp ge {since}"
            response = self._client.get(f"{TK_ODATA}/Document", params={
                "$filter": filters,
                "$select": "Id,Soort,DocumentNummer,Titel,Onderwerp,Datum,ContentType,"
                           "ContentLength,GewijzigdOp",
                "$orderby": "GewijzigdOp desc", "$top": self.page_size, "$skip": skip,
                "$count": "true", "$format": "json",
            }).json()
            rows = response.get("value") or []
            total = int(response.get("@odata.count") or 0)
            pages += 1
            if not rows:
                return
            for index, item in enumerate(rows):
                stub = _tk_stub(item, offset=skip + index, total=total)
                if stub:
                    yield stub
            if len(rows) < self.page_size:
                return
            skip += len(rows)

    def fetch(self, stub: Stub) -> Record | None:
        response = self._client.get(stub.raw_url or "")
        blob = response.content
        mime = (response.headers.get("content-type") or
                stub.hints.get("content_type") or "").split(";", 1)[0]
        if blob.startswith(b"%PDF"):
            ext = "pdf"
            text, needs_ocr, page_spans, engine = text_or_ocr(blob)
        elif blob.startswith(b"PK"):
            ext = "docx"
            extracted = extract_bytes(blob, ext=ext, mime=mime)
            text, needs_ocr, page_spans, engine = (
                extracted.text, extracted.needs_ocr, extracted.page_spans, extracted.engine)
        elif "html" in mime:
            ext = "html"
            blob, text = _content_html(blob)
            needs_ocr, page_spans, engine = False, None, "html-strip"
        else:
            return None
        if len((text or "").strip()) < 100:
            return None
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.PREPARATORY,
            title=stub.title, court="Tweede Kamer", decision_date=stub.hint_date,
            language="nl", source_language="nl", landing_url=stub.landing_url,
            raw_bytes=blob, raw_ext=ext, text=text.strip(),
            extracted_via=ExtractedVia.STRUCTURED,
            extra={"document_url": stub.raw_url,
                   "document_number": stub.hints.get("document_number"),
                   "document_kind": stub.hints.get("kind"), "needs_ocr": needs_ocr,
                   "page_spans": page_spans, "extraction_engine": engine,
                   "contenthash": stub.hints.get("contenthash")},
        )
