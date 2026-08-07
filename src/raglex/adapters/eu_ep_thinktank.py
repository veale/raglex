"""EPRS and the policy departments — the Parliament's own research, which no API serves.

The Open Data API's document-type vocabulary has ``STUDY``, ``STUDY_BRIEFING``,
``STUDY_DEPTH_ANALYSIS`` and ``EP_SUPPORTING_ANALYSE`` in it, and no endpoint returns a
single one of them. The material exists only behind the Think Tank's advanced search,
which is a server-rendered page. So this adapter reads that page — which
``robots.txt`` permits (it disallows ``/thinktank/*/pdf/search.html``, the results-PDF
generator, and nothing else under ``/thinktank/``), and which the documents' own licence
covers: every one carries "reuse … authorised under a Creative Commons Attribution 4.0
International (CC-BY 4.0) licence".

**Discovery is windowed by date**, which is the only axis the search exposes that
partitions the archive: ``startDate``/``endDate`` in ``dd/MM/yyyy``, back to 30 September
1989. Windows are walked newest-first and each is paged to exhaustion, so an incremental
run stops at its cursor within a window or two while a backfill walks the lot.

**The result count on the page is not the number of results.** It over-reports, by a
margin that varies with the window — 126 claimed against 116 reachable for one June
window, 17 against 10 for the whole of 1995, and 0 for a single-page day. Splitting a
window does not recover the difference (halving that June window gave 52+74 claimed,
42+64 walked, and a union *smaller* than the whole-window walk), and paging past the
first empty page returns nothing however far you go. The pages themselves are
well-behaved: ten per page, disjoint, and an empty page is a reliable end. So the walk
trusts the pages and ignores the count — and deliberately does NOT publish it as
``feed_total``, because a total that is wrong by an unknown margin produces a worse
progress bar than no total at all (see `job-authoring.md`).

**Text comes from the XML where there is one.** These documents are published as JATS —
the journal-article standard — alongside the PDF, so a briefing arrives with its section
hierarchy and its endnotes intact rather than as a page-ordered flattening. The PDF is
the fallback, and for the older material the only option.

The metadata is the point of the exercise, and most of it is on the document's own page
rather than in the XML: publication type, authors (internal and external are different
fields), policy areas, EuroVoc keywords, geographical areas — each with the **facet code**
the search itself uses, so a keyword held here is the same token the Think Tank filters
by. The XML's own ``eurovoc`` group is a literal "placeholder" in every document sampled;
the page is where the subject terms actually are.
"""

from __future__ import annotations

import html as _html
import re
from datetime import date, timedelta
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats import parse

HOST = "https://www.europarl.europa.eu"
SEARCH = f"{HOST}/thinktank/en/research/advanced-search"
DOCUMENT = f"{HOST}/thinktank/en/document"

#: The Think Tank serves the search only to something that looks like a browser, and only
#: once the cookie banner has been answered. Both are honest: the header names RagLex, and
#: the cookie records a choice rather than evading one.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
                   "Gecko/20100101 Firefox/128.0 (+RagLex legal-research corpus)"),
    "Accept": "*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cookie": "europarlcookiepolicysagreement=0",
}

#: The archive's own floor, from the search form's ``minValue``.
ARCHIVE_START = date(1989, 9, 30)

#: ``EPRS_BRI(2026)789356``, ``04A_FT(2017)N51055``, ``DG-4-JOIN_ET(1995)165643`` — the
#: id shape changed twice in thirty years and all three are still in the index.
_DOC_ID = re.compile(r"/thinktank/en/document/([A-Za-z0-9][\w.\-]*\(\d{4}\)[A-Za-z]?\d+)")
_RESULT = re.compile(
    r'class="es_document-title[^>]*>\s*<a href="/thinktank/en/document/([^"]+)"[^>]*>'
    r'\s*(?:<span[^>]*>)?(.*?)(?:</span>)?\s*</a>.*?'
    r'es_document-subtitle-documenttype[^>]*>(.*?)</span>.*?'
    r'es_document-subtitle-date[^>]*>(.*?)</span>',
    re.S)
#: One "About this document" panel opens with this class. The panels are read by
#: SPLITTING on it rather than by matching a closing boundary: the divs are deeply
#: nested and identical, so any terminator that worked for the middle panels dropped the
#: last one — which cost every document its geographical areas.
_META_MARKER = "es_other-links-item"
_META_HEADING = re.compile(r"<h5[^>]*>(.*?)</h5>", re.S)
_FACET_LINK = re.compile(
    r'advanced-search\?([A-Za-z]+)=([^"&]*)"[^>]*>.*?<span class="t-x">(.*?)</span>', re.S)
#: A commissioned study names the people who wrote it ABOVE the metadata panels and in
#: plain prose, because they are outside contractors with no entry in the author index.
#: They are the authority behind the document and belong on it all the same.
_EXTERNAL_AUTHORS = re.compile(
    r"<strong>\s*External author[s]?\s*</strong>\s*</p>\s*<p>(.*?)</p>", re.S | re.I)
#: Both schemes appear — the 2015 studies still link RegData over plain http.
_ASSET = re.compile(r'https?://www\.europarl\.europa\.eu/RegData/[^"\'\s<>]+', re.I)
#: The document page's own header — the authoritative title, type and date for a record
#: reached by id rather than through a search window.
_PRODUCT_NAME = re.compile(r'class="[^"]*es_product-name[^"]*"[^>]*>(.*?)</span>', re.S)
_PRODUCT_TYPE = re.compile(
    r'es_product-subtitle.*?<strong>(.*?)</strong>', re.S)
_PRODUCT_DATE = re.compile(
    r'class="[^"]*es_product-published-date[^"]*"[^>]*>\s*([\d]{2}-[\d]{2}-[\d]{4})', re.S)

#: The id's prefix says which service wrote it, which is worth keeping: an EPRS briefing
#: and a commissioned policy-department study are different kinds of authority.
SERVICES = {
    "EPRS": "European Parliamentary Research Service",
    "IPOL": "Policy Department, Internal Policies",
    "EXPO": "Policy Department, External Policies",
    "CASP": "Policy Department, Citizens' Rights and Constitutional Affairs",
    "ECTI": "Policy Department, Economic, Scientific and Quality of Life",
    "CCLA": "Policy Department, Cohesion and Structural Policies",
    "STOA": "Panel for the Future of Science and Technology",
    "04A": "Fact Sheets on the European Union",
    # Before the 2000s reorganisation the research service was DG IV, and its papers are
    # still in the index under the committee they were written for.
    "DG-4": "Directorate-General for Research (pre-2004)",
    "DG-2": "Directorate-General for Committees and Delegations (pre-2004)",
}


def _clean(fragment: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def document_url(doc_id: str) -> str:
    # The parentheses in the id are legal in a path and the site's own links leave them
    # unescaped; quoting them yields a 404.
    return f"{DOCUMENT}/{doc_id}"


def service_of(doc_id: str) -> tuple[str, str]:
    """``("EPRS", "European Parliamentary Research Service")`` from the id's prefix."""
    stem = (doc_id or "").split("_", 1)[0].upper()
    for key, label in SERVICES.items():
        if stem == key or stem.startswith(key + "-") or stem.endswith("-" + key):
            return key, label
    return stem, stem or "European Parliament"


def parse_results(html: str) -> list[dict]:
    """Title, type and date for each result block, in page order."""
    out: list[dict] = []
    for doc_id, title, kind, when in _RESULT.findall(html):
        out.append({"doc_id": _html.unescape(doc_id), "title": _clean(title),
                    "publication_type": _clean(kind), "date": _clean(when)})
    if out:
        return out
    # a layout change would silently empty every window, so fall back to the bare ids
    return [{"doc_id": d} for d in dict.fromkeys(_DOC_ID.findall(html))]


def parse_date(value: str | None) -> date | None:
    """``26-06-2026`` (the listing) or ``26/06/2026`` (the form)."""
    m = re.match(r"^\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\s*$", value or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def parse_about(html: str) -> dict[str, list[dict]]:
    """The "About this document" panels → ``{"Keyword": [{"code": …, "label": …}, …]}``.

    Each value keeps the facet CODE beside its label, because the code is what the search
    itself filters on: a document held with keyword ``0811`` can be lined up against the
    Think Tank's own "cooperation policy" listing without a name lookup."""
    out: dict[str, list[dict]] = {}
    for block in html.split(_META_MARKER)[1:]:
        heading = _META_HEADING.search(block)
        if not heading:
            continue
        label = _clean(heading.group(1))
        if not label:
            continue
        body = block[heading.end():]
        values = [{"facet": facet, "code": _html.unescape(code), "label": _clean(text)}
                  for facet, code, text in _FACET_LINK.findall(body)]
        if values:
            out.setdefault(label, []).extend(values)
            continue
        # "External author" is plain text — a semicolon-separated list of the people a
        # commissioned study was written by, who have no facet in the index.
        plain = _clean(re.sub(r"(?s)<(script|style).*?</\1>", " ", body))
        if plain:
            out.setdefault(label, []).extend(
                {"code": None, "label": part.strip()}
                for part in re.split(r";|\band\b", plain) if part.strip())
    external = _EXTERNAL_AUTHORS.search(html)
    if external:
        names = [_clean(part) for part in re.split(r";|\band\b", external.group(1))]
        out.setdefault("External author", []).extend(
            {"code": None, "label": name} for name in names if name)
    return out


def choose_asset(assets: list[str], suffix: str, language: str = "en") -> str | None:
    """The English asset of one kind. Two naming schemes are in use: ``…_EN.pdf`` for
    studies and briefings, ``doc_en.pdf`` for the Fact Sheets."""
    wanted = [a for a in assets if a.lower().endswith(suffix)]
    for pattern in (f"_{language.upper()}{suffix}", f"doc_{language.lower()}{suffix}"):
        for asset in wanted:
            if asset.endswith(pattern):
                return asset
    return wanted[0] if len(wanted) == 1 else None


def month_windows(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Whole months from ``end`` back to ``start``, newest first."""
    cursor = end
    while cursor >= start:
        first = cursor.replace(day=1)
        yield (max(first, start), cursor)
        cursor = first - timedelta(days=1)


class EPThinkTankAdapter(BaseAdapter):
    source = "eu-ep-thinktank"
    #: Deliberately unhurried: this is a courtesy crawl of a public institution's website,
    #: not an API with a published budget, and discovery alone is ~4,000 page reads.
    min_interval = 1.2

    def __init__(self, *, client: RateLimitedClient | None = None,
                 years: str | None = None, window_days: int | None = None,
                 document_ids: str | None = None, max_pages_per_window: int = 200,
                 language: str = "en") -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)
        self.language = (language or "en").lower()
        self.window_days = int(window_days) if window_days else None
        self.max_pages_per_window = max(1, int(max_pages_per_window))
        self.document_ids = tuple(
            x.strip() for x in str(document_ids).split(",") if x.strip()
        ) if document_ids else ()
        self.years = _year_span(years)

    # ---- discovery ---------------------------------------------------------------

    def _page(self, start: date, end: date, page: int) -> str:
        params = {"textualSearch": "",
                  "startDate": start.strftime("%d/%m/%Y"),
                  "endDate": end.strftime("%d/%m/%Y")}
        if page > 1:
            params["page"] = page
        resp = self._client.get(SEARCH, params=params, headers=BROWSER_HEADERS)
        return (resp.content or b"").decode("utf-8", "ignore")

    def _windows(self) -> Iterator[tuple[date, date]]:
        start, end = ARCHIVE_START, date.today()
        if self.years:
            start = max(start, date(self.years[0], 1, 1))
            end = min(end, date(self.years[1], 12, 31))
        if not self.window_days:
            yield from month_windows(start, end)
            return
        cursor = end
        span = timedelta(days=self.window_days - 1)
        while cursor >= start:
            yield (max(cursor - span, start), cursor)
            cursor = cursor - span - timedelta(days=1)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.document_ids:
            for doc_id in self.document_ids:
                yield Stub(stable_id=f"ep/thinktank/{doc_id}",
                           landing_url=document_url(doc_id),
                           raw_url=document_url(doc_id), hints={"doc_id": doc_id})
            return
        cutoff = parse_date(since) or (
            date.fromisoformat(str(since)[:10]) if since else None)
        windows = 0
        seen: set[str] = set()
        for start, end in self._windows():
            # newest-first, so an incremental run is one or two windows and stops
            if cutoff and end < cutoff:
                return
            for page in range(1, self.max_pages_per_window + 1):
                try:
                    html = self._page(start, end, page)
                except FetchError:
                    break
                rows = parse_results(html)
                if not rows:
                    break          # a page with no result blocks IS the end of the window
                for row in rows:
                    doc_id = row.get("doc_id")
                    if not doc_id or doc_id in seen:
                        continue
                    seen.add(doc_id)
                    when = parse_date(row.get("date"))
                    if cutoff and when and when < cutoff:
                        continue
                    yield Stub(
                        stable_id=f"ep/thinktank/{doc_id}",
                        landing_url=document_url(doc_id),
                        raw_url=document_url(doc_id),
                        title=row.get("title") or None,
                        hint_date=when,
                        hints={"doc_id": doc_id,
                               "publication_type": row.get("publication_type"),
                               "watermark": when.isoformat() if when else None,
                               "window": f"{start.isoformat()}..{end.isoformat()}"},
                    )
            windows += 1
            if max_pages is not None and windows >= max_pages:
                return

    # ---- fetch --------------------------------------------------------------------

    def _get(self, url: str) -> bytes | None:
        try:
            resp = self._client.get(url, headers=BROWSER_HEADERS)
        except FetchError:
            return None
        content = resp.content or b""
        return content if getattr(resp, "status_code", 200) < 400 and content else None

    def fetch(self, stub: Stub) -> Record | None:
        doc_id = stub.hints.get("doc_id") or stub.stable_id.rsplit("/", 1)[-1]
        page = self._get(document_url(doc_id))
        if not page:
            return None
        html = page.decode("utf-8", "ignore")
        about = parse_about(html)
        assets = list(dict.fromkeys(_ASSET.findall(html)))
        service_code, service = service_of(doc_id)

        parsed = None
        raw: bytes | None = None
        raw_ext = fmt = None

        xml_url = choose_asset(assets, ".xml", self.language)
        if xml_url:
            blob = self._get(xml_url)
            # Two schemas, and which one it is depends on the series: the briefings and
            # studies are JATS, the Fact Sheets have their own FTU DTD. Try both rather
            # than mapping the id prefix — the series membership is not always in the id.
            for name in ("jats-article", "ep-factsheet"):
                candidate = parse(name, blob) if blob else None
                if candidate and candidate.text:
                    raw, raw_ext, fmt, parsed = blob, "xml", name, candidate
                    break

        pdf_url = choose_asset(assets, ".pdf", self.language)
        needs_ocr = False
        if parsed is None and pdf_url:
            blob = self._get(pdf_url)
            if blob and blob.startswith(b"%PDF"):
                from ..extraction import extract_bytes

                extracted = extract_bytes(blob, ext="pdf", mime="application/pdf")
                if (extracted.text or "").strip():
                    from ..formats.base import ParsedDoc

                    raw, raw_ext, fmt = blob, "pdf", "pdf"
                    parsed = ParsedDoc(text=extracted.text)
                else:
                    # The pre-2000 papers were scanned, not typeset: the PDF is real and
                    # has no text layer at all. Say so, so the OCR queue can pick it up
                    # rather than the document reading as simply unavailable.
                    needs_ocr = True

        # The page header is authoritative for all three, and is the ONLY source of them
        # when a document is fetched by id rather than reached through a search window —
        # without it every targeted fetch landed undated.
        head_title = _PRODUCT_NAME.search(html)
        head_type = _PRODUCT_TYPE.search(html)
        head_date = _PRODUCT_DATE.search(html)
        title = (_clean(head_title.group(1)) if head_title else None) or stub.title \
            or (parsed.title if parsed else None) or doc_id
        when = (parse_date(head_date.group(1)) if head_date else None) \
            or stub.hint_date or (parsed.decision_date if parsed else None)

        def values(*labels: str) -> list[str]:
            out: list[str] = []
            for label in labels:
                out += [v["label"] for v in about.get(label, []) if v.get("label")]
            return list(dict.fromkeys(out))

        def codes(label: str) -> list[str]:
            return [v["code"] for v in about.get(label, []) if v.get("code")]

        publication_type = ((_clean(head_type.group(1)) if head_type else None)
                            or stub.hints.get("publication_type")
                            or (values("Publication type") or [None])[0]
                            or (parsed.metadata.get("publication_type") if parsed else None))
        keywords = values("Keyword")
        extra: dict = {
            "jurisdiction": "eu",
            "ep_document_id": doc_id,
            "service": service, "service_code": service_code,
            "publication_type": publication_type,
            "authors": values("Author", "External author"),
            "author_codes": codes("Author"),
            "policy_areas": values("Policy area"),
            "policy_area_codes": codes("Policy area"),
            "keywords": keywords,
            "keyword_codes": codes("Keyword"),
            "geographical_areas": values("Geographical area"),
            "committees": values("Committee"),
            "format": fmt,
            "pdf_url": pdf_url, "xml_url": xml_url,
            "licence": "CC-BY-4.0",
        }
        if parsed and parsed.metadata.get("pe_number"):
            extra["pe_number"] = parsed.metadata["pe_number"]
        # A briefing about one instrument ("Understanding the AI act") supplies the home
        # for its own bare "Article 6" references; a survey of a policy field must not.
        from .eu_consumer_guidance import title_default_instrument

        default_instrument = title_default_instrument(title)
        if default_instrument:
            extra["citation_default_instrument"] = default_instrument

        tags = ["european-parliament", "ep-research"]
        if service_code:
            tags.append(service_code.lower())
        if publication_type:
            tags.append(re.sub(r"[^a-z0-9]+", "-", publication_type.lower()).strip("-"))

        if parsed is None:
            # The page exists and its metadata is real; only the text could not be had.
            # Keep it as a node so the citator and the subject facets still see it.
            extra["metadata_only"] = True
            if needs_ocr:
                extra["needs_ocr"] = True
            return Record(
                source=self.source, stable_id=stub.stable_id, doc_type=DocType.COMMENTARY,
                title=title, language=self.language, source_language=self.language,
                decision_date=when, landing_url=document_url(doc_id),
                raw_bytes=page, raw_ext="html", extracted_via=ExtractedVia.SCRAPE,
                topic_tags=tags, extra=extra)

        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.COMMENTARY,
            title=title, language=self.language, source_language=self.language,
            decision_date=when, landing_url=document_url(doc_id),
            raw_bytes=raw, raw_ext=raw_ext, text=parsed.text,
            segments=list(parsed.segments or []),
            extracted_via=ExtractedVia.STRUCTURED if fmt == "jats-article"
            else ExtractedVia.SCRAPE,
            topic_tags=tags, extra=extra)


def _year_span(spec: str | None) -> tuple[int, int] | None:
    m = re.match(r"^\s*(\d{4})\s*(?:-\s*(\d{4}))?\s*$", str(spec or ""))
    if not m:
        return None
    a = int(m.group(1))
    return (a, int(m.group(2)) if m.group(2) else a)
