"""EDPB adapter — the European Data Protection Board's document register (§1.9/§4a).

Two harvest surfaces, two source keys:

**Documents** (``edpb``): every publication under ``/documents/{type}/…`` — guidelines,
recommendations, opinions, binding decisions, statements, reports, letters, adequacy
opinions, the lot. Discovery is the **sitemap** (``sitemap.xml?page=N``, ~6 pages),
which lists every page with a ``lastmod`` timestamp — so a full enumeration costs six
requests and the incremental cursor is the lastmod. A held page whose lastmod moved is
re-fetched (the change signal rides in ``hints["contenthash"]``), which is exactly how a
consultation draft becomes the adopted final version *in place*: same page, same
stable_id, new PDF — the §1.9 classifier then flips status → adopted on the re-classify.

Each document page is server-rendered: the H1 title, the publication-type label, the
date, the version status ("Final version" / consultation), the downloadable files with
labels, and the related-topic tags are all parsed into metadata; the first PDF is the
document body (text via the §5c extractors).

**One-stop-shop register** (``edpb-oss``): the Art 60 final-decision register at
``/registers/register-of-final-one-stop-shop-decisions_en`` — ~2,600 national DPA
decisions, 22 per page. Each entry carries an **EDPBI identifier**
(``EDPBI:LU:OSS:D:2026:3920`` — ECLI-shaped: lead-SA country, year, serial), the
decision date, the lead SA, the concerned SAs, topic tags, and the **main legal
reference** (GDPR articles) — which becomes an ``interprets`` edge to the GDPR
(32016R0679) with the article as the pinpoint, so every register decision links to the
provisions it applies. The lead SA becomes the ``court`` (``dpa-lu``), separating the
register by DPA. The serial is the incremental cursor (the register lists newest
first). Register PDFs are frequently **scans without a text layer** — those are
detected (no extractable text on image-bearing pages) and OCR'd via tesseract when
available; otherwise flagged ``needs_ocr`` so the gap is visible, never silent.

**Blocking**: europa.eu sits behind a WAF that can start refusing after sustained
scraping. Every fetch here treats a 403 — and an HTML body where a PDF was requested
(the challenge page) — as a *rate limit*, not an item failure: the batch stops, the
watermark stays put, nothing is written off, and the run resumes cleanly next tick.
The default pacing is deliberately slow (one request / 6s).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError, RateLimitException
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)

BASE_URL = "https://www.edpb.europa.eu"
GDPR_CELEX = "32016R0679"

# A browser-shaped UA — europa.eu's WAF treats the default library UA as a bot.
_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "Gecko/20100101 Firefox/128.0")}


def waf_get(client, source: str, url: str, *, expect_pdf: bool = False):
    """Fetch from a europa.eu property, mapping the WAF's refusals to a rate limit
    (401/403, or an HTML body where a PDF was requested = the challenge page) so the
    batch stops with the cursor intact instead of writing items off. Shared by the
    EDPB and A29WP adapters — same WAF, same failure modes."""
    resp = client.get(url, headers=_HEADERS, raise_for_4xx=False)
    status = getattr(resp, "status_code", 200)
    if status in (401, 403):
        # the WAF is refusing us, not telling us about this item — stop the batch
        raise RateLimitException(source)
    if status in (404, 410):
        raise FetchError(f"{source}: HTTP {status} for {url}", transient=False)
    if status >= 400:
        raise FetchError(f"{source}: HTTP {status} for {url}", transient=True)
    if expect_pdf:
        body = resp.content or b""
        ctype = str(resp.headers.get("content-type", "")) if hasattr(resp, "headers") else ""
        if body[:5].lstrip()[:1] == b"<" or ("html" in ctype and "pdf" not in ctype):
            # an HTML body where a PDF lives is the WAF's challenge page
            raise RateLimitException(source)
    return resp

# Sitemap section → doc_type. Everything not listed is regulator soft law → GUIDANCE.
SECTION_DOCTYPE: dict[str, DocType] = {
    "edpb-binding-decisions": DocType.DECISION,        # Art 65 binding decisions
    "legislative-opinion": DocType.OPINION,            # Art 70 opinions on draft law
    "opinion-of-the-board-art-64": DocType.OPINION,    # Art 64 consistency opinions
    "adequacy": DocType.OPINION,                       # opinions on adequacy decisions
    "legal-study-by-external-suppliers": DocType.COMMENTARY,  # commissioned studies
}

# Main register: /documents/{section}/{slug}_en — the section may carry digits
# ("opinion-of-the-board-art-64" is 257 documents) or be absent entirely (a few
# root-level pages). The Coordinated Supervision Committee subsite nests one level
# deeper: /csc/documents/{section}/{slug}_en — 280 more documents.
_SITEMAP_DOC = re.compile(
    r"https://www\.edpb\.europa\.eu/(?P<csc>csc/)?documents/"
    r"(?:(?P<section>[a-z0-9-]+)/)?(?P<slug>[^/<\s]+?)_en$"
)


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    section: str
    slug: str
    url: str
    lastmod: str | None
    csc: bool = False


def parse_sitemap(xml_bytes: bytes) -> list[SitemapEntry]:
    """English document-page entries of one sitemap page (pure) — the main
    ``/documents/…`` register plus the CSC subsite's ``/csc/documents/…``."""
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[SitemapEntry] = []
    for url in root.findall(f"{ns}url"):
        loc = (url.findtext(f"{ns}loc") or "").strip()
        m = _SITEMAP_DOC.match(loc)
        if not m:
            continue
        csc = bool(m.group("csc"))
        section = m.group("section") or ""
        out.append(SitemapEntry(
            section=("csc-" + section) if csc else section,
            slug=m.group("slug"), url=loc,
            lastmod=(url.findtext(f"{ns}lastmod") or "").strip() or None,
            csc=csc,
        ))
    return out


# ── document page (server-rendered Drupal) ──────────────────────────────────
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_META_LABEL = re.compile(r'class="document-full__meta"[^>]*>(.*?)</', re.S)
_DATE = re.compile(r'class="document-full__date"[^>]*>\s*(?:<time[^>]*>)?([^<]+)', re.S)
_TIME_ISO = re.compile(r'class="document-full__date"[^>]*>\s*<time datetime="([^"]+)"', re.S)
_VERSION = re.compile(r'class="document-full__version"[^>]*>(.*?)</', re.S)
_CONSULT = re.compile(r'class="document-full__public-consultation-link"\s+href="([^"]+)"')
_TOPIC = re.compile(r'relevant-topics-list-item-link[^>]*>\s*#?([^<]+)')
_FILE = re.compile(r'<a[^>]+href="(/system/files/[^"]+)"[^>]*>(.*?)</a>', re.S)
_OG_DESC = re.compile(r'<meta (?:property|name)="og:description" content="([^"]*)"')
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def _clean(fragment: str | None) -> str | None:
    import html as _html

    if fragment is None:
        return None
    t = re.sub(r"<[^>]+>", " ", fragment)
    t = re.sub(r"\s+", " ", _html.unescape(t)).strip()
    return t or None


def _long_date(s: str | None) -> date | None:
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", (s or "").strip())
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def parse_document_page(html: str) -> dict:
    """Everything the page header states about the document (pure): title, publication-
    type label, date, version status, the consultation-draft link, every downloadable
    file with its label, topic tags, and the og:description."""
    h1 = _H1.search(html)
    iso = _TIME_ISO.search(html)
    d = _long_date(_clean(_DATE.search(html).group(1)) if _DATE.search(html) else None)
    if d is None and iso:
        try:
            d = datetime.fromisoformat(iso.group(1).replace("Z", "+00:00")).date()
        except ValueError:
            d = None
    files, seen = [], set()
    for m in _FILE.finditer(html):
        href, label = m.group(1), _clean(m.group(2)) or ""
        if href in seen:
            continue  # each file appears twice (title link + download button)
        seen.add(href)
        files.append({"href": href, "label": re.sub(r"^Download\s+", "", label)})
    return {
        "title": _clean(h1.group(1)) if h1 else None,
        "type_label": _clean(_META_LABEL.search(html).group(1)) if _META_LABEL.search(html) else None,
        "date": d,
        "version_status": _clean(_VERSION.search(html).group(1)) if _VERSION.search(html) else None,
        "consultation_url": (_CONSULT.search(html).group(1) if _CONSULT.search(html) else None),
        "files": files,
        "topics": [t.strip() for t in _TOPIC.findall(html) if t.strip()],
        "description": _clean(_OG_DESC.search(html).group(1)) if _OG_DESC.search(html) else None,
    }


# ── one-stop-shop register ───────────────────────────────────────────────────
_EDPBI = re.compile(r"EDPBI:(?P<cc>[A-Z]{2}):OSS:D:(?P<year>\d{4}):(?P<serial>\d+)")
_OSS_TEASER_SPLIT = re.compile(r'class="foss-decision-teaser"')
_OSS_TIME = re.compile(r'<time datetime="([^"]+)"')
_OSS_LEAD = re.compile(r'lead-sa.*?member-country-token__icon-flag-(?P<cc>[a-z]{2})', re.S)
_OSS_CONCERNED = re.compile(r'concerned-sa[^"]*".*?flag-(?P<cc>[a-z]{2})', re.S)
_OSS_PDF = re.compile(r'href="(/system/files/[^"]+\.pdf)"')
_OSS_LEGAL = re.compile(r'main-legel-ref-value[^>]*>\s*([^<]+)')
_OSS_TOPIC = re.compile(r'relevant-topics-list-item-link[^>]*>\s*#?([^<]+)')
_PAGER_LAST = re.compile(r'\?page=(\d+)')


@dataclass(frozen=True, slots=True)
class OSSDecision:
    edpbi: str
    country: str          # lead SA, lower-case ISO code
    year: int
    serial: int
    decided: date | None
    pdf_url: str | None
    concerned: tuple[str, ...] = ()
    legal_refs: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()


def parse_oss_register(html: str) -> tuple[list[OSSDecision], int]:
    """One register listing page (pure) → its decisions + the highest page number the
    pager advertises (0-based; 0 means this is the only page)."""
    last_page = max((int(p) for p in _PAGER_LAST.findall(html)), default=0)
    chunks = _OSS_TEASER_SPLIT.split(html)[1:]
    out: list[OSSDecision] = []
    for chunk in chunks:
        m = _EDPBI.search(chunk)
        if not m:
            continue
        decided = None
        t = _OSS_TIME.search(chunk)
        if t:
            try:
                decided = datetime.fromisoformat(t.group(1).replace("Z", "+00:00")).date()
            except ValueError:
                decided = None
        lead = _OSS_LEAD.search(chunk)
        pdf = _OSS_PDF.search(chunk)
        concerned = tuple(dict.fromkeys(
            cc for cc in _OSS_CONCERNED.findall(chunk) if not (lead and cc == lead.group("cc"))))
        out.append(OSSDecision(
            edpbi=m.group(0),
            country=(lead.group("cc") if lead else m.group("cc").lower()),
            year=int(m.group("year")),
            serial=int(m.group("serial")),
            decided=decided,
            pdf_url=pdf.group(1) if pdf else None,
            concerned=concerned,
            legal_refs=tuple(dict.fromkeys(r.strip() for r in _OSS_LEGAL.findall(chunk) if r.strip())),
            topics=tuple(dict.fromkeys(t.strip() for t in _OSS_TOPIC.findall(chunk) if t.strip())),
        ))
    return out, last_page


# ── OCR fallback for scanned register PDFs ───────────────────────────────────
def looks_unocrd(text: str | None, page_count: int) -> bool:
    """A PDF with essentially no extractable text across its pages is an image-only
    scan. The threshold is deliberately low — a stamped cover page can carry a few
    dozen chars of metadata text while the decision body is still all image."""
    if page_count <= 0:
        return False
    return len((text or "").strip()) < max(120, 40 * page_count)


def ocr_pdf(data: bytes, *, dpi: int = 200, max_pages: int = 80) -> str | None:
    """Tesseract the PDF's pages (rasterised via PyMuPDF). Returns None when the OCR
    stack (pytesseract + the tesseract binary + Pillow) isn't available — the caller
    records ``needs_ocr`` instead of failing the harvest."""
    try:
        import io

        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image

        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 — any missing piece → no OCR available
        return None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for page in doc[:max_pages]:
            pix = page.get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            pages.append(pytesseract.image_to_string(img))
        return "\n\n".join(pages).strip() or None
    except Exception:  # noqa: BLE001 — a corrupt scan must not kill the batch
        return None


class EDPBAdapter(BaseAdapter):
    """``register=False`` → the documents corpus (source ``edpb``); ``register=True``
    → the one-stop-shop decisions register (source ``edpb-oss``)."""

    source = "edpb"
    min_interval = 6.0   # slow drip — europa.eu's WAF punishes sustained fast crawls
    requires_js = False
    requires_proxy = False

    def __init__(self, *, register: bool | str = False, sections: str | None = None,
                 client: RateLimitedClient | None = None) -> None:
        self.register = bool(register) and str(register).lower() not in ("0", "false", "no")
        if self.register:
            self.source = "edpb-oss"
        # optional csv filter: only these /documents/{section}/ types
        self.sections = tuple(s.strip().lower() for s in (sections or "").split(",") if s.strip()) or None
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    # -- polite fetch with WAF-block detection --------------------------------
    def _get(self, url: str, *, expect_pdf: bool = False):
        return waf_get(self._client, self.source, url, expect_pdf=expect_pdf)

    # -- discovery -------------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.register:
            yield from self._discover_register(since)
        else:
            yield from self._discover_documents(since)

    def _discover_documents(self, since: str | None) -> Iterator[Stub]:
        """Walk the whole sitemap (~6 pages — fixed cost, so ``max_pages`` doesn't
        apply), keep entries whose lastmod moved past the cursor, oldest first. The
        sitemap is NOT sorted by lastmod, so the crawl filters rather than early-stops;
        yielding oldest-first makes the cursor a resumable drip (an interrupted run
        continues where it left off, because the watermark only reached what was
        actually processed)."""
        entries: list[SitemapEntry] = []
        for page in range(1, 13):
            try:
                resp = self._get(f"{BASE_URL}/sitemap.xml?page={page}")
            except FetchError:
                break
            batch = parse_sitemap(resp.content)
            if not batch and page > 1:
                break
            entries.extend(batch)
        if self.sections:
            entries = [e for e in entries if e.section in self.sections]
        if since:
            entries = [e for e in entries if e.lastmod and e.lastmod > since]
        entries.sort(key=lambda e: e.lastmod or "")
        for e in entries:
            yield Stub(
                # csc slugs live under their own prefix so a main-register document
                # with the same slug can never collide
                stable_id=f"edpb/csc/{e.slug}" if e.csc else f"edpb/{e.slug}",
                landing_url=e.url,
                raw_url=e.url,
                hint_date=_iso_date(e.lastmod),
                hints={"section": e.section,
                       **({"watermark": e.lastmod, "contenthash": e.lastmod} if e.lastmod else {})},
            )

    _REGISTER_URL = f"{BASE_URL}/registers/register-of-final-one-stop-shop-decisions_en"

    def _discover_register(self, since: str | None) -> Iterator[Stub]:
        """The register lists newest-serial first. Incremental: stop at the first page
        holding nothing newer than the cursor. First run (no cursor): walk every page —
        the archive backfill; a rate-limit stop keeps the watermark unset, so the next
        run resumes (already-fetched decisions dedup before their PDFs are re-paid)."""
        page = 0
        seen: set[int] = set()
        stale_pages = 0
        while True:
            # A listing-page failure mid-walk propagates: the register is walked
            # newest-first, so returning quietly here would advance the cursor past
            # everything not yet reached — failing the run leaves the cursor intact.
            resp = self._get(self._REGISTER_URL + (f"?page={page}" if page else ""))
            decisions, last_page = parse_oss_register(
                resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes)
                else str(resp.content))
            if not decisions:
                return
            new = [d for d in decisions
                   if d.serial not in seen and (not since or f"{d.serial:08d}" > since)]
            seen.update(d.serial for d in decisions)
            for d in new:
                if not d.pdf_url:
                    continue
                yield Stub(
                    stable_id=f"edpb/oss/{d.year}/{d.serial}",
                    landing_url=self._REGISTER_URL,
                    raw_url=f"{BASE_URL}{d.pdf_url}",
                    hint_date=d.decided,
                    title=d.edpbi,
                    court=f"dpa-{d.country}",
                    hints={"oss": d, "watermark": f"{d.serial:08d}"},
                )
            # The register is only ROUGHLY serial-ordered (live page 0 shows 3920,
            # 3921, 3918, 3926…), so don't stop at the first page that dips below the
            # cursor — walk two further pages to catch out-of-order stragglers.
            if since:
                stale_pages = stale_pages + 1 if len(new) < len(decisions) else 0
                if stale_pages >= 3:
                    return
            if page >= last_page:
                return
            page += 1

    # -- fetch -------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        if self.register:
            return self._fetch_oss(stub)
        return self._fetch_document(stub)

    def _fetch_document(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        resp = self._get(stub.landing_url)
        html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else str(resp.content)
        meta = parse_document_page(html)
        section = stub.hints.get("section") or ""
        doc_type = SECTION_DOCTYPE.get(section, DocType.GUIDANCE)

        pdfs = [f for f in meta["files"] if f["href"].lower().endswith(".pdf")]
        main = next((f for f in pdfs if "_en" in f["href"].lower()), pdfs[0] if pdfs else None)
        raw, raw_ext, text = resp.content, "html", None
        needs_ocr = False
        if main:
            pdf = self._get(f"{BASE_URL}{main['href']}", expect_pdf=True)
            raw, raw_ext = pdf.content, "pdf"
            extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
            text = extracted.text
            if looks_unocrd(text, _pdf_pages(raw)):
                ocr = ocr_pdf(raw)
                if ocr:
                    text = ocr
                else:
                    needs_ocr = True
        if not text:
            text = extract_bytes(html.encode(), ext="html").text

        status = (meta["version_status"] or "").lower()
        extra = {
            "edpb_type": section or (meta["type_label"] or "").lower() or None,
            "url": stub.landing_url,
            "topics": meta["topics"],
            "description": meta["description"],
            "version_status": meta["version_status"],
            "consultation_url": meta["consultation_url"],
            "pdf_url": main["href"] if main else None,
            "other_files": [f for f in meta["files"] if not main or f["href"] != main["href"]],
            **({"contenthash": stub.hints["contenthash"]} if stub.hints.get("contenthash") else {}),
            **({"needs_ocr": True} if needs_ocr else {}),
            **({"consultation_draft": True} if "consultation" in status else {}),
        }
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=doc_type,
            title=meta["title"] or stub.stable_id,
            decision_date=meta["date"],
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=raw_ext,
            text=text or None,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["edpb"] + [_slug(t) for t in meta["topics"]],
            extra={k: v for k, v in extra.items() if v not in (None, [], "")},
        )

    def _fetch_oss(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        d: OSSDecision = stub.hints["oss"]
        resp = self._get(stub.raw_url, expect_pdf=True)
        raw = resp.content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = extracted.text
        needs_ocr = False
        if looks_unocrd(text, _pdf_pages(raw)):
            # many register PDFs are scans with no text layer — OCR or flag, never
            # silently store an empty document
            ocr = ocr_pdf(raw)
            if ocr:
                text = ocr
            else:
                needs_ocr = True

        # the register's "main legal reference" → interprets edges to the GDPR,
        # pinpointed to the article(s) the decision applies
        relations = [TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=f"{ref} GDPR",
            dst_id=GDPR_CELEX, dst_anchor=ref,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ) for ref in d.legal_refs]

        title = f"{d.edpbi} — {d.country.upper()} SA final decision"
        if d.legal_refs:
            title += f" ({', '.join(d.legal_refs[:3])})"
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=title,
            court=f"dpa-{d.country}",
            decision_date=d.decided,
            language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text or None,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["edpb-oss", f"dpa-{d.country}"] + [_slug(t) for t in d.topics],
            extra={k: v for k, v in {
                "edpbi": d.edpbi,
                "lead_sa": d.country,
                "concerned_sas": list(d.concerned),
                "legal_refs": list(d.legal_refs),
                "topics": list(d.topics),
                "pdf_url": d.pdf_url,
                # the EDPBI is how these decisions are cited — mint it as an alias so
                # a citing document's "EDPBI:LU:OSS:D:2026:3920" resolves here (§5b)
                "aliases": [d.edpbi.casefold()],
                **({"needs_ocr": True} if needs_ocr else {}),
            }.items() if v not in (None, [], "")},
        )


def _pdf_pages(data: bytes) -> int:
    try:
        import fitz

        return fitz.open(stream=data, filetype="pdf").page_count
    except Exception:  # noqa: BLE001
        return 0


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _iso_date(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None
