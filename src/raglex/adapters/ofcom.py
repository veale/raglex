"""Ofcom Online Safety Act regulatory documents adapter (§1.9/§4a).

Ofcom's hub of online-safety regulatory documents — the Codes of Practice, risk
assessment guidance, registers of risks, and the rest of the material implementing
the **Online Safety Act 2023** (``ukpga/2023/50``):

    https://www.ofcom.org.uk/online-safety/illegal-and-harmful-content/online-safety-regulatory-documents

The hub and both protecting-children / illegal-harms statement pages are scanned.
Collapsed accordions contain ordinary PDF links as well as download widgets.
The pages are server-rendered HTML: documents also sit under category headings as
``<a class="file-download" href="…/foo.pdf?v=NNNNNN">Title (status) • PDF • size •
date</a>``. Two signals drive supersession, exactly as the page presents it:

  - the **``?v=`` version token** — a PDF re-published at the same path gets a new
    token, so a changed token means the document was updated in place; and
  - the **status marker in the title** — "(Updated)" / "(superseded)" / "DRAFT" /
    "Issued". Ofcom keeps the old file (relabelled "(superseded)") alongside the new
    "(Updated)" one, both sharing a base title. We hold **both**, and mint a
    ``supersedes`` edge from the current version to each superseded one, so the
    version chain is navigable.

**Linking to the statute, both ways.** Every document ``interprets`` the Online
Safety Act 2023 (a base edge), plus a pinpointed edge for each Part/Chapter named in
its title ("Chapter 5 of Part 7", "Part 5 duties"). The PDF text is then mined by the
§5b extractor, which — now that the OSA is in the statute map — links every "section N
of the Online Safety Act 2023" to ``ukpga/2023/50`` at section granularity. The graph
reads both ways: from a document to the provisions it implements, and from an OSA
section to every Ofcom document that bears on it.

**Monitoring**: no incremental cursor (a curated list), so ``discover`` yields the
whole set each run; the pipeline dedups unchanged documents (same version token) and
re-fetches any whose token moved — surfacing a new/updated version the moment Ofcom
publishes it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
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

BASE_URL = "https://www.ofcom.org.uk"
PAGE_URL = (BASE_URL + "/online-safety/illegal-and-harmful-content/"
            "online-safety-regulatory-documents")
PAGE_URLS = (
    PAGE_URL,
    BASE_URL + "/online-safety/illegal-and-harmful-content/statement-protecting-children-from-harms-online",
    BASE_URL + "/online-safety/illegal-and-harmful-content/statement-protecting-people-from-illegal-harms-online",
)
OSA_ID = "ukpga/2023/50"  # the Online Safety Act 2023

_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) "
                           "Gecko/20100101 Firefox/152.0")}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
# "Part 5", "Part 7", "Chapter 5 of Part 7", "Part 3 Guidance" — OSA structural units.
_PART = re.compile(r"\b(Chapter\s+\d+\s+of\s+Part\s+\d+|Part\s+\d+|Chapter\s+\d+)\b", re.IGNORECASE)


class OfcomHTTP:
    """Chrome-TLS client for Ofcom's WAF, with the adapter's normal pacing.

    Ofcom started returning 403 to plain httpx in July 2026.  The pages remain
    server-rendered and need no browser; curl_cffi's Chrome handshake is sufficient.
    """

    def __init__(
        self,
        source: str,
        *,
        min_interval: float = 1.0,
        stealth_fetcher=None,
        session=None,
    ) -> None:
        self.source = source
        self.min_interval = min_interval
        self._last_request_at = 0.0
        self._session = session
        self._fallback = None
        self._stealth_fetcher = stealth_fetcher

    def _stealth_html(self, url: str, headers: dict | None):
        if self._stealth_fetcher is None:
            from ..scraping.fetcher import get_fetcher

            # When RagLex is linked to Scrapling MCP this resolves to the shared
            # Camoufox service; otherwise it retains the in-process stealth fallback.
            self._stealth_fetcher = get_fetcher(
                "stealth",
                source=self.source,
                min_interval=self.min_interval,
                requires_js=True,
            )
        page = self._stealth_fetcher.fetch(url, headers=headers)
        if page.status >= 400 or not page.html:
            raise FetchError(
                f"{self.source}: stealth HTTP {page.status} for {url}",
                transient=page.status >= 500,
            )

        @dataclass(slots=True)
        class _Response:
            content: bytes
            status_code: int = 200

        return _Response(page.html.encode("utf-8"))

    def get(self, url: str, **kwargs):
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()
        if self._session is None:
            try:
                from curl_cffi import requests as creq
            except ImportError:
                if self._fallback is None:
                    self._fallback = RateLimitedClient(
                        self.source, min_interval=self.min_interval
                    )
                return self._fallback.get(url, **kwargs)
            self._session = creq.Session(impersonate="chrome124", timeout=90)
        response = self._session.get(url, **kwargs)
        # The iMac's egress IP is blocked even with Chrome TLS.  HTML pages can
        # safely escalate to the already-linked Scrapling service.  PDFs stay on
        # the binary HTTP path because the MCP fetch contract returns HTML text.
        if (
            response.status_code in {403, 429, 503}
            and not urlsplit(url).path.lower().endswith(".pdf")
        ):
            return self._stealth_html(url, kwargs.get("headers"))
        if response.status_code >= 400:
            raise FetchError(
                f"{self.source}: HTTP {response.status_code} for {url}",
                transient=response.status_code >= 500,
            )
        return response


@dataclass(frozen=True, slots=True)
class OfcomDoc:
    href: str            # site-relative PDF path (no query)
    version: str | None  # the ?v= token
    raw_title: str       # the full anchor text
    title: str           # cleaned title (status + size/date suffix removed)
    base_title: str      # title minus a "(superseded)/(Updated)/…" qualifier, for grouping
    status: str          # current | superseded | draft
    size: str | None
    published: date | None
    category: str | None
    parts: tuple[str, ...]  # OSA Parts/Chapters named in the title


def _parse_date(text: str) -> date | None:
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+((?:19|20)\d{2})\b", text)
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def _parse_title(raw: str) -> tuple[str, str, str, str | None]:
    """(clean title, base title, status, size) from a raw anchor text like
    'Register of Risks (Updated) • PDF • 4.47 MB • 25 June 2026'."""
    # size, if present ("• PDF • 4.47 MB •" or "(PDF, 3.09 MB)")
    sm = re.search(r"(\d+(?:\.\d+)?\s*[KMG]B)", raw)
    size = sm.group(1) if sm else None
    # drop the metadata tail: everything from the first " • " bullet or a "(PDF, …)" note
    title = re.split(r"\s*•\s*", raw)[0]
    title = re.sub(r"\s*\(PDF,[^)]*\)\s*", " ", title).strip()
    low = title.lower()
    if "superseded" in low or "superseeded" in low:
        status = "superseded"
    elif title.lstrip().upper().startswith("DRAFT"):
        status = "draft"
    else:
        status = "current"
    # base title for grouping: strip trailing status qualifiers and a bare "(ICJG)" etc.
    base = re.sub(r"\s*\((?:updated|superseded|icjg|pdf[^)]*)\)\s*", " ", title, flags=re.IGNORECASE)
    # The hub also uses a bare, misspelled SUPERSEEDED prefix on issued codes.
    base = re.sub(r"^\s*(?:DRAFT|Issued|Final|Updated|Superseded|Superseeded)\s+", "", base,
                  flags=re.IGNORECASE)
    base = re.sub(r"\s+", " ", base).strip(" -–—")
    return title.strip(), base, status, size


def parse_page(html: str) -> list[OfcomDoc]:
    """Every regulatory document on the page (pure), each with its version token,
    status, size, date, category heading and any OSA Part it names."""
    from bs4 import BeautifulSoup

    # Accordion bodies are present in the source HTML even when collapsed. Ordinary
    # rich-text links carry most statement volumes; file-download alone loses them.
    soup = BeautifulSoup(html, "html.parser")
    out: list[OfcomDoc] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        url = urlsplit(anchor["href"])
        if url.netloc and url.netloc.lower() != "www.ofcom.org.uk":
            continue
        href = url.path
        if not href.startswith("/") or not href.lower().endswith(".pdf"):
            continue
        ver = (parse_qs(url.query).get("v") or [None])[0]
        if href in seen:
            continue
        raw = anchor.get_text(" ", strip=True) or str(anchor.get("title") or "").strip()
        if not raw:
            raw = href.rsplit("/", 1)[-1][:-4].replace("-", " ")
        seen.add(href)
        heading = anchor.find_previous(["h2", "h3", "h4"])
        title, base, status, size = _parse_title(raw)
        out.append(OfcomDoc(
            href=href, version=ver, raw_title=raw, title=title, base_title=base,
            status=status, size=size, published=_parse_date(raw),
            category=heading.get_text(" ", strip=True) if heading else None,
            parts=tuple(dict.fromkeys(re.sub(r"\s+", " ", p).title()
                                      for p in _PART.findall(raw))),
        ))
    return out


def _slug(href: str) -> str:
    """A stable id from the PDF path: the last two path segments (dir + filename),
    which distinguish a superseded copy (…/illegal-harms/x.pdf) from its updated
    replacement (…/illegal-harms/updates/x.pdf)."""
    parts = [p for p in href.split("?")[0].split("/") if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    stem = re.sub(r"\.pdf$", "", "/".join(tail), flags=re.IGNORECASE)
    # Ofcom publishes both x.pdf and x-.pdf for issued and draft codes. Keeping
    # the path's trailing hyphen prevents the draft overwriting the issued text.
    return "ofcom/" + re.sub(r"[^a-z0-9/]+", "-", stem.lower()).lstrip("-")


def supersession_edges(docs: list[OfcomDoc]) -> dict[str, list[str]]:
    """current-doc stable_id → [superseded stable_ids] within each base-title group.
    Only groups that actually contain a superseded member yield edges."""
    groups: dict[str, list[OfcomDoc]] = {}
    for d in docs:
        groups.setdefault(d.base_title.lower(), []).append(d)
    out: dict[str, list[str]] = {}
    for members in groups.values():
        superseded = [d for d in members if d.status == "superseded"]
        current = [d for d in members if d.status == "current"]
        if not superseded or not current:
            continue
        # the newest current version supersedes the old ones
        cur = max(current, key=lambda d: d.published or date.min)
        out[_slug(cur.href)] = [_slug(s.href) for s in superseded]
    return out


class OfcomOSAAdapter(BaseAdapter):
    source = "ofcom-osa"
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or OfcomHTTP(
            self.source, min_interval=self.min_interval
        )

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        docs: list[OfcomDoc] = []
        landing_pages: dict[str, str] = {}
        # These are complementary inventories, not pagination. A watch configured
        # with max_pages=1 must still examine all three, including older volumes.
        for page_url in PAGE_URLS:
            resp = self._client.get(page_url, headers=_HEADERS)
            html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else str(resp.content)
            page_docs = parse_page(html)
            if not page_docs:
                raise FetchError(f"{self.source}: no PDF links found on {page_url}")
            for d in page_docs:
                if d.href not in landing_pages:
                    docs.append(d)
                    landing_pages[d.href] = page_url
        supersedes = supersession_edges(docs)
        for d in docs:
            sid = _slug(d.href)
            yield Stub(
                stable_id=sid,
                landing_url=landing_pages[d.href],
                raw_url=BASE_URL + d.href + (f"?v={d.version}" if d.version else ""),
                hint_date=d.published,
                title=d.title,
                hints={"doc": d, "supersedes": supersedes.get(sid, []),
                       # the version token is the change signal — a new token at the same
                       # path means Ofcom re-published, so the held copy must re-fetch
                       **({"contenthash": d.version} if d.version else {})},
            )

    def fetch(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        d: OfcomDoc = stub.hints["doc"]
        resp = self._client.get(stub.raw_url, headers=_HEADERS)
        raw = resp.content
        extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
        text = extracted.text
        needs_ocr = False
        if not (text and text.strip()):
            from .edpb import ocr_pdf
            ocr = ocr_pdf(raw)
            text, needs_ocr = (ocr, False) if ocr else (text, True)

        relations: list[TypedRelation] = []
        # base link to the Online Safety Act, plus a pinpoint per Part/Chapter in the title
        relations.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string="Online Safety Act 2023", dst_id=OSA_ID,
            extracted_via=ExtractedVia.STRUCTURED, resolution_status=ResolutionStatus.PENDING))
        for part in d.parts:
            relations.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=f"{part} of the Online Safety Act 2023",
                dst_id=OSA_ID, dst_anchor=part,
                extracted_via=ExtractedVia.STRUCTURED, resolution_status=ResolutionStatus.PENDING))
        # supersedes edges to the older versions this document replaces
        for old in stub.hints.get("supersedes", []):
            relations.append(TypedRelation(
                relationship_type=RelationshipType.SUPERSEDES,
                raw_citation_string=old, dst_id=old,
                extracted_via=ExtractedVia.STRUCTURED, resolution_status=ResolutionStatus.PENDING))

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.GUIDANCE,
            title=d.title,
            court="Ofcom",
            decision_date=d.published,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf",
            text=text or None,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["ofcom", "online-safety", d.status],
            extra={k: v for k, v in {
                "issuer": "ofcom",
                "regime": OSA_ID,
                "status": d.status,
                "version": d.version,
                "size": d.size,
                "category": d.category,
                "base_title": d.base_title,
                "osa_parts": list(d.parts),
                "published": d.published.isoformat() if d.published else None,
                "pdf_url": stub.raw_url,
                "supersedes": stub.hints.get("supersedes", []),
                **({"contenthash": d.version} if d.version else {}),
                **({"needs_ocr": True} if needs_ocr else {}),
            }.items() if v not in (None, [], "")},
        )
