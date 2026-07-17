"""Ofcom Online Safety Act regulatory documents adapter (§1.9/§4a).

Ofcom's hub of online-safety regulatory documents — the Codes of Practice, risk
assessment guidance, registers of risks, and the rest of the material implementing
the **Online Safety Act 2023** (``ukpga/2023/50``):

    https://www.ofcom.org.uk/online-safety/illegal-and-harmful-content/online-safety-regulatory-documents

The page is plain server-rendered HTML: documents sit under H2 category headings as
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
from dataclasses import dataclass
from datetime import date
from typing import Iterator

from ..core.adapter import BaseAdapter
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
OSA_ID = "ukpga/2023/50"  # the Online Safety Act 2023

_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) "
                           "Gecko/20100101 Firefox/152.0")}

_DOWNLOAD = re.compile(
    r'<a[^>]+class="file-download"[^>]+href="(?P<href>/[^"]+?\.pdf)(?:\?v=(?P<v>\d+))?"[^>]*>'
    r'(?P<body>.*?)</a>', re.S)
_H2 = re.compile(r"<h2>(?P<h>.*?)</h2>", re.S)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
# "Part 5", "Part 7", "Chapter 5 of Part 7", "Part 3 Guidance" — OSA structural units.
_PART = re.compile(r"\b(Chapter\s+\d+\s+of\s+Part\s+\d+|Part\s+\d+|Chapter\s+\d+)\b", re.IGNORECASE)


def _clean(fragment: str) -> str:
    import html as _html

    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


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
    if "superseded" in low:
        status = "superseded"
    elif title.lstrip().upper().startswith("DRAFT"):
        status = "draft"
    else:
        status = "current"
    # base title for grouping: strip trailing status qualifiers and a bare "(ICJG)" etc.
    base = re.sub(r"\s*\((?:updated|superseded|icjg|pdf[^)]*)\)\s*", " ", title, flags=re.IGNORECASE)
    base = re.sub(r"^\s*(?:DRAFT|Issued|Final|Updated)\s+", "", base, flags=re.IGNORECASE)
    base = re.sub(r"\s+", " ", base).strip(" -–—")
    return title.strip(), base, status, size


def parse_page(html: str) -> list[OfcomDoc]:
    """Every regulatory document on the page (pure), each with its version token,
    status, size, date, category heading and any OSA Part it names."""
    # map each download's position to the nearest preceding H2 (its category)
    headings = [(m.start(), _clean(m.group("h"))) for m in _H2.finditer(html)]

    def category_at(pos: int) -> str | None:
        cat = None
        for start, h in headings:
            if start <= pos:
                cat = h
            else:
                break
        return cat

    out: list[OfcomDoc] = []
    seen: set[str] = set()
    for m in _DOWNLOAD.finditer(html):
        href, ver = m.group("href"), m.group("v")
        if href in seen:
            continue
        seen.add(href)
        raw = _clean(m.group("body"))
        if not raw:
            continue
        title, base, status, size = _parse_title(raw)
        out.append(OfcomDoc(
            href=href, version=ver, raw_title=raw, title=title, base_title=base,
            status=status, size=size, published=_parse_date(raw),
            category=category_at(m.start()),
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
    return "ofcom/" + re.sub(r"[^a-z0-9/]+", "-", stem.lower()).strip("-")


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
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        resp = self._client.get(PAGE_URL, headers=_HEADERS)
        html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else str(resp.content)
        docs = parse_page(html)
        supersedes = supersession_edges(docs)
        for d in docs:
            sid = _slug(d.href)
            yield Stub(
                stable_id=sid,
                landing_url=PAGE_URL,
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
            landing_url=PAGE_URL,
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
