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

import logging
from datetime import date, datetime
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub

log = logging.getLogger("raglex.adapters.uk_parl_committees")

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
                 publication_types: str | None = None, stealth_fetcher=None,
                 start_offset: int = 0) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)
        self._stealth = stealth_fetcher
        # see be_gba_decisions: emitting resume_offset obliges us to accept it back
        self.start_offset = max(0, int(start_offset or 0))
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
            skip, total = self.start_offset, None
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

    @staticmethod
    def report_url(url: str) -> str:
        """``.../325/report.htm`` → ``.../325/report.html``.

        Commons and Joint reports are one page named ``report.htm``, and the page that
        actually holds the text is ``report.html`` — the ``.htm`` form answers 200 with a
        9.5 KB cookie-consent shell and no report in it, which is worse than an error
        because it looks like a fetch that worked and yields a document with no text.

        LORDS papers do NOT follow that convention and must not be rewritten. Their
        ``additionalContentUrl`` is a numbered chapter page (``.../45/4502.htm``) whose
        ``.html`` sibling is the bare banner, so appending an ``l`` turns a page with
        content into one without. Their whole report is the PDF in
        ``additionalContentUrl2`` (``.../45/45.pdf``), which needs a bytes-capable fetch
        past Cloudflare — the HTML-only stealth path returns nothing for it."""
        if not url.endswith("report.htm") or "publications.parliament.uk" not in url:
            return url
        return url + "l"

    def _stealth_html(self, url: str) -> bytes | None:
        """publications.parliament.uk sits behind a Cloudflare JS challenge ("Just a
        moment... Enable JavaScript and cookies to continue"), so a plain client gets 403
        however it is dressed, and the API's own /Document/{id}/{format} endpoint returns
        500 for the same publication. A real browser passes the challenge, which is what
        the shared Camoufox service is for."""
        if self._stealth is None:
            from ..scraping.fetcher import get_fetcher
            self._stealth = get_fetcher(
                "stealth", source=self.source, min_interval=self.min_interval,
                requires_js=False)
        try:
            page = self._stealth.fetch(url)
        except Exception as exc:  # noqa: BLE001 — a walled page is a miss, not a crash
            # …but say WHY. A page Cloudflare won't give up and a fetch service that is
            # refusing every request both come back as None, and the second one is an
            # outage: 2,386 committee reports harvested, then four days of silent misses
            # while the scrapling container sat at its pid ceiling. One log line is the
            # difference between noticing that and not.
            log.warning("%s: stealth fetch failed for %s: %s", self.source, url, exc)
            return None
        html = getattr(page, "html", None)
        if not html or getattr(page, "status", 200) >= 400:
            return None
        return html.encode("utf-8") if isinstance(html, str) else html

    def _whole_report(self, stub: Stub, referer: str | None) -> bytes | None:
        """The paper's WHOLE report (``additionalContentUrl2``), through the browser.

        This is how a LORDS report is read at all. Their ``additionalContentUrl`` is one
        numbered chapter page, so the HTML route yields a fragment at best. It is
        Cloudflare-walled and cannot be had by any plain request, so it needs the
        bytes-capable navigation (see BrowserBytesFetcher), cleared via the paper's own
        page.

        Usually a PDF, but not always: the older Lords papers point this at a ``.htm``
        whole-report page instead. Take whatever it serves and let the caller sniff the
        type off the bytes — insisting on ``%PDF`` here threw away the papers that are
        published as HTML, which is the opposite of the point.
        """
        target = (stub.hints.get("pdf_url") or "").strip()
        if not target or "publications.parliament.uk" not in target:
            return None
        from ..scraping.fetcher import get_bytes_fetcher

        fetcher = get_bytes_fetcher()
        if not fetcher.available():
            log.warning("%s: no browser in this image; cannot read %s",
                        self.source, target)
            return None
        return fetcher.fetch_bytes(target, referer_url=referer)

    def fetch(self, stub: Stub) -> Record | None:
        url = self.report_url(stub.raw_url or "")
        if not url or "/api/Publications/" in url:
            return None          # metadata-only row with no published text to read
        blob: bytes | None = None
        if "publications.parliament.uk" not in url:
            try:
                blob = self._client.get(url).content
            except FetchError:
                blob = None
        # A Lords paper's HTML is one chapter of the report; its PDF is the whole thing,
        # so prefer the PDF outright rather than storing a fragment as the report.
        is_lords = str(stub.hints.get("house") or "").strip().lower() == "lords"
        if blob is None and is_lords:
            blob = self._whole_report(stub, referer=url)
        # Cloudflare-walled host: go straight to the browser rather than spend a request
        # earning a 403 first.
        if blob is None:
            blob = self._stealth_html(url)
        # Commons/Joint papers published only as a PDF (or whose HTML is the consent
        # shell) still have a whole report to read — fall back to it rather than skip.
        if not blob:
            blob = self._whole_report(stub, referer=url)
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
