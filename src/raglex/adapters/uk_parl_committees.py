"""UK parliamentary committee publications — reports, government responses, correspondence.

Select-committee reports are the working preparatory material behind a great deal of UK
law: they read the statute as it stands, take evidence on how it operates, and the
Government's reply is published as a numbered paper in the same series. Both sides of that
exchange cite legislation and case law heavily, which is what makes them worth holding
beside the instruments they discuss.

Identity is the **paper number** — ``HC 69 (2026-27)``, ``HL Paper 45 (2026-27)`` — because
that is how a committee report is cited, and it is stable where the API's own integer id
is an internal handle. A publication with no paper number (much correspondence, minutes)
falls back to that internal id, which is honest: those genuinely have no citable number.

The register is large (8,507 reports alone across 16 publication types), so the default
sweep is the types that carry argument — reports, government responses, special reports,
correspondence and scrutiny evidence — rather than attendance statistics and gender-balance
tables, which are numbers with no legal content at all. ``publication_types`` overrides it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub

BASE = "https://committees-api.parliament.uk"
PUBLICATIONS = f"{BASE}/api/Publications"
PAGE_SIZE = 50
# From the API's own enum (PublicationQuerySortOrder). Inventing a plausible-looking
# value here is an HTTP 400, and a 400 inside discovery is silent: the sweep simply
# yields nothing and reads as "no publications".
SORT_NEWEST_FIRST = "PublicationDateDescending"

# The register's own type ids (GET /api/PublicationType). The default set is the material
# that actually argues about law; the excluded ones — attendance statistics (9), gender
# balance (11), declarations of interest (7), agendas (5) — are administrivia that would
# add thousands of documents citing nothing.
TYPE_REPORT = 1
TYPE_GOVERNMENT_RESPONSE = 2
TYPE_CORRESPONDENCE = 3
TYPE_SCRUTINY_EVIDENCE = 8
TYPE_SPECIAL_REPORT = 12
TYPE_EUROPEAN_SCRUTINY = 16
DEFAULT_TYPES = (TYPE_REPORT, TYPE_GOVERNMENT_RESPONSE, TYPE_SPECIAL_REPORT,
                 TYPE_CORRESPONDENCE, TYPE_SCRUTINY_EVIDENCE, TYPE_EUROPEAN_SCRUTINY)

DOC_TYPES = {
    TYPE_REPORT: DocType.PREPARATORY,
    TYPE_SPECIAL_REPORT: DocType.PREPARATORY,
    TYPE_GOVERNMENT_RESPONSE: DocType.PREPARATORY,
    TYPE_EUROPEAN_SCRUTINY: DocType.PREPARATORY,
    TYPE_SCRUTINY_EVIDENCE: DocType.NOTE,
    TYPE_CORRESPONDENCE: DocType.NOTE,
}


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def paper_number(item: dict) -> tuple[str | None, str | None]:
    """``("HC 69", "2026-27")`` — the citation and the session it belongs to.

    A paper number repeats every session (there is an HC 69 in most of them), so the
    session is part of the identity, not decoration."""
    for key in ("hcNumber", "hlPaper"):
        block = item.get(key)
        if isinstance(block, dict) and block.get("number"):
            return str(block["number"]).strip(), str(
                block.get("sessionDescription") or "").strip() or None
    return None, None


def stable_id(item: dict) -> str:
    number, session = paper_number(item)
    if number and session:
        slug = number.lower().replace(" ", "-").replace("paper-", "")
        return f"uk/parl/committee/{slug}-{session}"
    return f"uk/parl/committee/pub-{item.get('id')}"


def publication_stubs(payload: dict) -> list[Stub]:
    out: list[Stub] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        committee = item.get("committee") or {}
        kind = item.get("type") or {}
        number, session = paper_number(item)
        # The report's own HTML on publications.parliament.uk. The API serves metadata;
        # the text lives there, and that page is also what a citation points a reader at.
        url = str(item.get("additionalContentUrl") or "").strip()
        out.append(Stub(
            stable_id=stable_id(item),
            landing_url=url or f"{BASE}/api/Publications/{item['id']}",
            raw_url=url or f"{BASE}/api/Publications/{item['id']}",
            title=" ".join(str(item.get("description") or "").split()),
            court="uk-parliament",
            hint_date=_as_date(item.get("publicationStartDate")),
            hints={
                "publication_id": item.get("id"),
                "publication_type_id": (kind or {}).get("id"),
                "publication_type": (kind or {}).get("name"),
                "committee": committee.get("name"),
                "house": committee.get("house"),
                "paper_number": number,
                "session": session,
                "pdf_url": str(item.get("additionalContentUrl2") or "").strip() or None,
                "documents": [d.get("documentId") for d in (item.get("documents") or [])
                              if isinstance(d, dict) and d.get("documentId")],
                "watermark": str(item.get("publicationStartDate") or "")[:10] or None,
            },
        ))
    return out


class UKCommitteePublicationsAdapter(BaseAdapter):
    source = "uk-parl-committees"
    min_interval = 0.4

    def __init__(self, *, client: RateLimitedClient | None = None,
                 publication_types: str | None = None, stealth_fetcher=None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)
        self._stealth = stealth_fetcher
        self._types = tuple(
            int(x) for x in str(publication_types).replace(" ", "").split(",") if x
        ) if publication_types else DEFAULT_TYPES

    # ---- discovery -------------------------------------------------------------

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Newest-first per type, so an incremental run stops at its cursor.

        The API sorts by publication date descending, filters server-side on
        ``StartDate``, and reports ``totalResults`` — so keep-current is one bounded
        request per type, not a crawl."""
        for type_id in self._types:
            params: dict = {"PublicationTypeIds": type_id, "Take": PAGE_SIZE,
                            "SortOrder": SORT_NEWEST_FIRST}
            if since:
                params["StartDate"] = str(since)[:10]
            skip, total = 0, None
            pages = 0
            while True:
                params["Skip"] = skip
                try:
                    payload = self._client.get(PUBLICATIONS, params=params).json()
                except (FetchError, ValueError):
                    break
                if total is None:
                    total = int(payload.get("totalResults") or 0)
                rows = publication_stubs(payload)
                if not rows:
                    break
                for stub in rows:
                    stub.hints["feed_total"] = total
                    stub.hints["resume_offset"] = skip
                    yield stub
                skip += len(rows)
                pages += 1
                if total is not None and skip >= total:
                    break
                if max_pages is not None and pages >= max_pages:
                    break

    # ---- fetch -----------------------------------------------------------------

    def _stealth_html(self, url: str) -> bytes | None:
        """publications.parliament.uk answers a plain client with HTTP 403, and the API's
        own /Document/{id}/{format} endpoint currently returns 500 for the same
        publication — so neither obvious route reaches the text. The shared Camoufox
        service does, which is what it is there for."""
        if self._stealth is None:
            from ..scraping.fetcher import get_fetcher
            self._stealth = get_fetcher(
                "stealth", source=self.source, min_interval=self.min_interval,
                requires_js=False)
        try:
            page = self._stealth.fetch(url)
        except Exception:  # noqa: BLE001 — a walled page is a miss, not a crash
            return None
        html = getattr(page, "html", None)
        if not html or getattr(page, "status", 200) >= 400:
            return None
        return html.encode("utf-8") if isinstance(html, str) else html

    def fetch(self, stub: Stub) -> Record | None:
        url = stub.raw_url
        if not url or "/api/Publications/" in url:
            return None          # metadata-only row with no published text to read
        blob: bytes | None = None
        try:
            blob = self._client.get(url).content
        except FetchError:
            blob = None
        if blob is None:
            blob = self._stealth_html(url)
        if not blob:
            return None
        from ..extraction import extract_bytes
        ext = "pdf" if blob.startswith(b"%PDF") else "html"
        try:
            extracted = extract_bytes(
                blob, ext=ext, mime="application/pdf" if ext == "pdf" else "text/html")
        except ValueError:
            return None
        text = (extracted.text or "").strip()
        if len(text) < 400:
            return None
        type_id = stub.hints.get("publication_type_id")
        number, session = stub.hints.get("paper_number"), stub.hints.get("session")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DOC_TYPES.get(int(type_id or 0), DocType.PREPARATORY),
            title=stub.title,
            court="uk-parliament",
            decision_date=stub.hint_date,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=blob, raw_ext=ext, text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["uk-parliament", "committee",
                        *( [str(stub.hints.get("house")).lower()]
                           if stub.hints.get("house") else [] )],
            extra={
                "jurisdiction": "uk",
                "committee": stub.hints.get("committee"),
                "house": stub.hints.get("house"),
                "publication_type": stub.hints.get("publication_type"),
                "paper_number": number,
                "session": session,
                "citation": f"{number} ({session})" if number and session else None,
                # A committee's whole output is not legal material: attendance tables,
                # minutes and covering letters cite nothing. Storing them is fine;
                # putting them in front of a researcher is not. The gate keeps anything
                # the grammars find no statute or authority in out of retrieval.
                "require_recognized_legal_citation": True,
            },
        )
