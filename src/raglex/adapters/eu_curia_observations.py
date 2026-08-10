"""Published written observations from the CJEU's InfoCuria register.

InfoCuria is an Angular application, but its public ``elastic-connector`` search
service exposes the case/document metadata used by the page and its public ``blob``
service serves the underlying PDFs.  Written observations are deliberately absent
from EUR-Lex (the UI disables its EUR-Lex button); the document code used by CURIA is
``OBSRP_PUB``.

Discovery is a small full walk of the server-side document-type filter.  It is not
watermark-stopped: observations can be published well after their filing date, so a
date cursor would miss late releases.  Stable logical document ids make the repeated
weekly walk cheap and idempotent.  Only documents filed in the most recent five years
are emitted by default, one record per language rendition.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterator
from urllib.parse import quote

from ..core.adapter import BaseAdapter, option_int
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

SEARCH_URL = "https://infocuriaws.curia.europa.eu/elastic-connector/search"
BLOB_BASE = "https://infocuriaws.curia.europa.eu/blob/download-file"
AFFAIR_PAGE = "https://infocuria.curia.europa.eu/tabs/affair"
DOC_TYPE = "OBSRP_PUB"

_CASE_RE = re.compile(
    r"^(?P<court>[CTF])[-\N{NON-BREAKING HYPHEN}\N{EN DASH}]?"
    r"(?P<number>\d+)/(?P<year>\d{2,4})",
    re.IGNORECASE,
)


def _as_date(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _cutoff(years: int, today: date | None = None) -> date:
    """Calendar-year cutoff, including 29 February without an invalid replacement."""
    today = today or date.today()
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def judgment_celex(case_number: str | None) -> str | None:
    """The decision CELEX which the normal EU case-number grammar also mints.

    ``C-492/23`` becomes ``62023CJ0492`` and ``T-8/93`` becomes ``61993TJ0008``.
    The CELEX alias maintained by ``eu-cellar`` resolves this to the held ECLI node.
    """
    m = _CASE_RE.match((case_number or "").strip())
    if not m:
        return None
    year = int(m.group("year"))
    if year < 100:
        year += 2000 if year <= date.today().year % 100 else 1900
    return f"6{year:04d}{m.group('court').upper()}J{int(m.group('number')):04d}"


def _landing_url(case_number: str) -> str:
    term = quote(f'"{case_number}"', safe="")
    return f"{AFFAIR_PAGE}?searchTerm={quote(term, safe='')}&publishedId={quote(case_number)}"


def _search_payload(page: int, page_size: int) -> dict:
    return {
        "searchTerm": "",
        "multiSearchTerms": [],
        "sortTermList": [{"sortDirection": "DESC", "sortTerm": "INTRODUCTION_DATE"}],
        "pagination": {"pageNumber": page, "pageSize": page_size},
        "language": "EN",
        "tabName": "affair",
        "isAllTabsRequest": False,
        "publishedId": "",
        "ecli": "",
        "usualName": "",
        "logicDocId": "",
        "repJurExpand": True,
        "filtersValue": [],
        "advancedFiltersValue": [{
            "field": "typeDoc",
            "values": [DOC_TYPE],
            "valuesWithFullHierarchy": [],
            "isMatchAll": False,
        }],
        "isSearchExact": False,
        "searchSources": ["document", "metadata"],
    }


def observation_stubs(payload: dict, *, cutoff: date) -> list[Stub]:
    """Normalise one search response (kept pure for fixture-level tests)."""
    out: list[Stub] = []
    seen: set[tuple[str, str]] = set()
    for case_hit in payload.get("searchHits") or []:
        case = case_hit.get("content") or {}
        case_number = case.get("publishedId") or case.get("publishedAffId")
        case_title = next((x.get("en") for x in case.get("usualNameML") or []
                           if x.get("en")), None)
        documents = (((case_hit.get("innerHits") or {}).get("document") or {})
                     .get("searchHits") or [])
        for hit in documents:
            doc = hit.get("content") or {}
            if DOC_TYPE not in str(doc.get("docTypeCode") or ""):
                continue
            filed = _as_date(doc.get("docDate"))
            # One live row currently says 2054 for a 2024 case. Do not let an upstream
            # typo escape the bounded backfill or advance a corpus watermark decades.
            if not filed or filed < cutoff or filed > date.today():
                continue
            logical = str(doc.get("logicDocId") or "").removeprefix("id_")
            if not logical or not case_number:
                continue
            renditions = doc.get("groupByLogicalId") or []
            for rendition in renditions:
                language = str(rendition.get("docLang") or "").lower()
                formats = {str(x).upper() for x in rendition.get("formats") or []}
                key = (logical, language)
                if not language or "PDF" not in formats or key in seen:
                    continue
                seen.add(key)
                raw_url = f"{BLOB_BASE}/{logical}/{language.upper()}/PDF"
                author = doc.get("author") or doc.get("authorCode") or "unknown party"
                title = f"Written observations of {author} in {case_number}"
                if case_title:
                    title += f" ({case_title})"
                out.append(Stub(
                    stable_id=f"curia/observations/{logical}/{language}",
                    landing_url=_landing_url(case_number),
                    raw_url=raw_url,
                    hint_date=filed,
                    title=title,
                    court="Court of Justice of the European Union",
                    hints={
                        "logic_doc_id": f"id_{logical}",
                        "language": language,
                        "case_number": case_number,
                        "case_title": case_title,
                        "author": author,
                        "author_code": doc.get("authorCode"),
                        "procedure_id": doc.get("idProcedure") or case.get("procedureId"),
                        "document_type_code": doc.get("docTypeCode"),
                        "watermark": filed.isoformat(),
                    },
                ))
    return out


class EUCuriaObservationsAdapter(BaseAdapter):
    """Five-year backfill and full-walk watch for public written observations."""

    source = "eu-curia-observations"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, client: RateLimitedClient | None = None,
                 years: int | str | None = 5, page_size: int = 100,
                 max_ocr_pages: int | str | None = 200) -> None:
        self.years = max(1, option_int(years, 5))
        self.page_size = min(100, max(1, option_int(page_size, 100)))
        self.max_ocr_pages = max(0, option_int(max_ocr_pages, 200))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # Deliberately ignore ``since``. A document can be made public long after its
        # filing date; the six-page full walk catches that and stable ids dedupe it.
        cutoff = _cutoff(self.years)
        page = 0
        emitted: set[str] = set()
        while max_pages is None or page < max_pages:
            try:
                response = self._client.request(
                    "POST", SEARCH_URL, json=_search_payload(page, self.page_size),
                    headers={"Origin": "https://infocuria.curia.europa.eu",
                             "Referer": "https://infocuria.curia.europa.eu/"})
                payload = response.json()
            except (FetchError, ValueError):
                return
            hits = payload.get("searchHits") or []
            for stub in observation_stubs(payload, cutoff=cutoff):
                if stub.stable_id not in emitted:
                    emitted.add(stub.stable_id)
                    yield stub
            page += 1
            total = int(payload.get("totalHits") or 0)
            if not hits or page * self.page_size >= total:
                return

    def fetch(self, stub: Stub) -> Record | None:
        try:
            response = self._client.get(
                stub.raw_url or "",
                headers={"Origin": "https://infocuria.curia.europa.eu",
                         "Referer": stub.landing_url or AFFAIR_PAGE})
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        raw = response.content or b""
        if not raw.startswith(b"%PDF-"):
            return None

        text, needs_ocr, spans, engine = text_or_ocr(raw, max_pages=self.max_ocr_pages)
        segments = [Segment(label=f"p. {n}", char_start=start, char_end=end, kind="page")
                    for n, start, end in spans]
        case_number = stub.hints.get("case_number")
        target = judgment_celex(case_number)
        relation = TypedRelation(
            relationship_type=RelationshipType.RELATED_TO,
            raw_citation_string=case_number,
            dst_id=target,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        )
        language = stub.hints.get("language") or "und"
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.PREPARATORY,
            title=stub.title,
            court=stub.court,
            decision_date=stub.hint_date,
            language=language,
            source_language=language,
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text or None,
            segments=segments,
            relations=[relation],
            topic_tags=["eu", "cjeu", "written-observations", language],
            extracted_via=ExtractedVia.STRUCTURED,
            extra={
                **stub.hints,
                "jurisdiction": "eu",
                "document_kind": "written observations",
                "pdf_url": stub.raw_url,
                "related_case_celex": target,
                "needs_ocr": needs_ocr,
                "extraction_engine": engine,
            },
        )
