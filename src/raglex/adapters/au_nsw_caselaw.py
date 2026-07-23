"""NSW Caselaw — live incremental case law (the currency layer for the bulk corpus).

Raglex already holds Australian decisions in bulk via ``au-caselaw`` (the Open
Australian Legal Corpus JSONL — a periodic snapshot). What that snapshot cannot give
is *currency*: yesterday's judgments. This adapter closes that gap for New South Wales
by harvesting **caselaw.nsw.gov.au** the same way the OALC creator's ``nsw_caselaw``
scraper does — the ``/browse/list`` JSON index → each ``/decision/{id}`` page — but as
an **incremental, newest-first crawl** that stops at the watermark, so a weekly watch
pulls only what's new.

**Identity is shared with the bulk corpus.** The index carries the medium neutral
citation (``mnc``, e.g. "[2024] NSWSC 1"), so the stable_id is the same
``nswsc/2024/1`` slug :func:`au_case_slug` mints — meaning a live-harvested decision
*is the same node* as its eventual OALC-snapshot copy (they dedup), and importing one
resolves the "[2024] NSWSC 1" citations the corpus already holds pending. A decision
with no neutral citation gets a stable surrogate keyed on its caselaw.nsw.gov.au id.

**Body.** The judgment HTML (``<div class="judgment">``) is the text; a PDF-only
decision (``See Attachment (PDF)``) falls back to its ``/asset`` PDF, extracted (and
OCR-flagged if it is a scan). Restricted / withdrawn stubs are skipped.

The Federal Court and High Court are the same shape (an incremental, date-sorted index
→ a per-decision fetch) over their own search backends; see the module notes at the
foot for how those two slot in beside this one.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from .au_caselaw import _AU_NEUTRAL, au_case_slug

BASE = "https://www.caselaw.nsw.gov.au"
LIST_URL = f"{BASE}/browse/list?page={{page}}"


def _court_code(mnc: str | None) -> str | None:
    """The neutral-citation court token ("[2024] NSWSC 1" → "nswsc") — the court bucket,
    matching the first segment of the ``au_case_slug`` id."""
    m = _AU_NEUTRAL.search(mnc or "")
    return m.group("court").lower() if m else None


def _surrogate(decision_id: str) -> str:
    return f"au-case/nsw/{decision_id}"


_JUDGMENT_DIV = re.compile(r'<div class="judgment"[^>]*>(.*?)</div>\s*(?:<div|</div>|$)', re.S)
_PDF_ATTACH = re.compile(r'<a href="/asset/([^"]+)">See Attachment \(PDF\)</a>')


def _clean_title(entry: dict) -> str:
    title = " ".join((entry.get("title") or "").split())
    mnc = entry.get("mnc") or ""
    return f"{title} {mnc}".strip() or mnc or entry.get("id", "")


def _is_listable(entry: dict) -> bool:
    """Drop restricted decisions and placeholder rows (the OALC creator's filter)."""
    if entry.get("restricted"):
        return False
    title = " ".join((entry.get("title") or "").lower().split())
    return "decision number not in use" not in title and "decision restricted" not in title


class NSWCaselawAdapter(BaseAdapter):
    source = "au-nsw-caselaw"
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    # a safety ceiling on a first (no-watermark) backfill walk
    _MAX_PAGES_BACKFILL = 400

    def __init__(self, *, client: RateLimitedClient | None = None,
                 max_pages: int | None = None) -> None:
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)
        self._max_pages_cfg = max_pages

    # -- discovery (newest-first, stop at the watermark) ----------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        cap = max_pages or self._max_pages_cfg or (None if since else self._MAX_PAGES_BACKFILL)
        page = 0
        while True:
            try:
                resp = self._client.get(LIST_URL.format(page=page))
            except FetchError:
                return
            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError):
                return
            rows = data.get("searchableDecisions") or []
            if not rows:
                return
            reached_watermark = False
            for entry in rows:
                if not _is_listable(entry):
                    continue
                d = _entry_date(entry)
                iso = d.isoformat() if d else None
                # newest-first: once we cross the cursor, everything after is older
                if since and iso and iso <= since:
                    reached_watermark = True
                    break
                mnc = entry.get("mnc")
                slug = au_case_slug(mnc)
                did = str(entry.get("id") or "")
                if not (slug or did):
                    continue
                yield Stub(
                    stable_id=slug or _surrogate(did),
                    landing_url=f"{BASE}/decision/{did}",
                    title=_clean_title(entry),
                    court=_court_code(mnc),
                    hint_date=d,
                    hints={"id": did, "mnc": mnc, "date": iso,
                           **({"watermark": iso} if iso else {})},
                )
            page += 1
            if reached_watermark or (cap is not None and page >= cap):
                return

    # -- fetch -----------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        try:
            resp = self._client.get(stub.landing_url)
        except FetchError:
            return None
        html = resp.text
        raw, raw_ext, mime = html.encode("utf-8"), "html", "text/html"
        needs_ocr = False

        pdf = _PDF_ATTACH.search(html)
        if pdf:
            try:
                asset = self._client.get(f"{BASE}/asset/{pdf.group(1)}")
            except FetchError:
                asset = None
            if asset is not None:
                raw, raw_ext, mime = asset.content, "pdf", "application/pdf"
                try:
                    text = extract_bytes(raw, ext="pdf", mime=mime).text
                except Exception:  # noqa: BLE001 — a corrupt/scanned PDF must not kill the batch
                    text = None
                needs_ocr = not (text and text.strip())
            else:
                text = None
        else:
            body = _JUDGMENT_DIV.search(html)
            fragment = (body.group(1) if body else html).encode("utf-8")
            text = extract_bytes(fragment, ext="html", mime="text/html").text

        if not text and not needs_ocr:
            return None

        mnc = stub.hints.get("mnc")
        d = stub.hints.get("date")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=stub.title or stub.stable_id,
            court=stub.court or _court_code(mnc) or "nsw",
            decision_date=_iso_date(d),
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=raw_ext,
            text=text or None,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["au-caselaw", "nsw"],
            extra={k: v for k, v in {
                "jurisdiction": "new_south_wales",
                "neutral_citation": mnc,
                "caselaw_nsw_id": stub.hints.get("id"),
                "url": stub.landing_url,
                # the neutral citation is how this decision is cited — mint it so the
                # bare "[2024] NSWSC 1" reference resolves here (and unifies with the
                # OALC bulk node under the same slug)
                "aliases": [mnc.casefold()] if mnc else None,
                **({"needs_ocr": True} if needs_ocr else {}),
            }.items() if v not in (None, [], "")},
        )


def _entry_date(entry: dict) -> date | None:
    txt = entry.get("decisionDateText")
    if not txt:
        return None
    try:
        return datetime.strptime(txt, "%d %B %Y").date()
    except ValueError:
        return None


def _iso_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


# ── Federal Court & High Court: the same pattern, different backends ──────────
# Both slot in as sibling adapters (``au-fca``, ``au-hca``) with the identical
# incremental shape used above — a date-ordered index walked newest-first to the
# watermark, then a per-decision fetch — reusing ``au_case_slug`` so their ids
# (``fca/2024/255``, ``fcafc/2024/12``, ``hca/2024/1``) unify with the OALC bulk and
# resolve pending citations. What differs is only the fetch tier:
#
# * **au-fca** — the Funnelback search at ``search.judgments.fedcourt.gov.au`` already
#   sorts by ``adate``; request it *descending* and page by ``start_rank`` until a
#   result predates the cursor. Two quirks the OALC ``federal_court_of_australia``
#   scraper documents must be carried over: judgment pages are **windows-1250**, not
#   UTF-8, and a decision with no HTML body exposes an "Original Word Document" link
#   whose **DOCX** is the text (mammoth/DOCX extraction; a legacy ``.doc`` is skipped).
#   Norfolk Island SC decisions ride this database but are jurisdiction ``norfolk_island``.
#
# * **au-hca** — the ``eresources.hcourt.gov.au`` SERP across its collections
#   (``col=0/1/2`` + the historical set), filtered by year; walk the most-recent year(s)
#   for currency. A decision is HTML unless a download button is present, in which case
#   it is a **PDF/DOCX/RTF** to extract (OCR the PDF scans, as ``edpb``/this module do).
#
# Kept out of this file so its clean HTTP/JSON path isn't entangled with the heavier
# DOCX/OCR fetchers; they are the obvious next two adapters once NSW is proven live.
