"""High Court of Australia — full-text judgments from the HCA site (hcourt.gov.au).

The HCA judgments index is server-rendered, and the judgments themselves are published
as DOCX (and PDF) on a static path. The site sits behind a WAF that fingerprints the
TLS handshake and admits only a real Chrome — plain HTTP is 403'd and the stealth
tier's Firefox is blocked — but **curl_cffi's Chrome impersonation clears it with no
browser**. So the whole chain runs over ordinary HTTP requests:

1. **Discovery** — the listing ``…/judgments-1998-current?items_per_page=100&page=N``
   (the *non-faceted* form; the ``f[0]=d:YEAR`` facet is the one path the WAF blocks)
   lists 100 judgments a page, newest-first, with the case name, medium neutral
   citation, coram, date and a link to each judgment's detail page. ~14 pages cover
   1998→present; incremental runs stop at the watermark.
2. **Fetch** — the detail page carries the judgment's **DOCX** (and PDF) URL under
   ``/sites/default/files/eresources/…``; the DOCX is downloaded (static, unguarded)
   and extracted (§5c DOCX provider) into the body text.

Identity is the neutral-citation slug :func:`au_case_slug` mints (``hca/2026/22``), so a
harvested judgment unifies with the OALC bulk and resolves the "[2026] HCA 22" citations
the corpus already holds. ``path=`` imports a listing page saved from a browser instead
of fetching it live (the detail/DOCX are still fetched over HTTP). A judgment whose DOCX
can't be reached falls back to a metadata stub (identity + coram + "view on HCA" link).
"""

from __future__ import annotations

import html as _html
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

from ..core.adapter import BaseAdapter
from ..core.models import DocType, ExtractedVia, Record, Stub
from .au_caselaw import au_case_slug

BASE = "https://www.hcourt.gov.au"
INDEX = BASE + "/cases-and-judgments/judgments/judgments-1998-current"
LISTING_PAGE = INDEX + "?items_per_page=100&page={page}"
_IMPERSONATE = "chrome124"


# ── Chrome-TLS HTTP (curl_cffi), with an httpx fallback for the open static files ────
class _ChromeHTTP:
    """A tiny Chrome-impersonating GET, so the WAF that only trusts real Chrome lets us
    through. Falls back to httpx (fine for the unguarded ``/sites/default/files`` DOCX,
    not for the WAF'd pages) when curl_cffi isn't installed."""

    def __init__(self) -> None:
        self._sess = None
        self._httpx = None

    def get(self, url: str) -> tuple[int, bytes]:
        try:
            from curl_cffi import requests as creq
            if self._sess is None:
                self._sess = creq.Session(impersonate=_IMPERSONATE, timeout=60)
            r = self._sess.get(url)
            return r.status_code, r.content
        except ImportError:
            import httpx
            if self._httpx is None:
                self._httpx = httpx.Client(timeout=60, follow_redirects=True,
                                           headers={"User-Agent": "Mozilla/5.0 Chrome/124"})
            r = self._httpx.get(url)
            return r.status_code, r.content


# ── pure parsing ─────────────────────────────────────────────────────────────
def _field(row: str, pattern: str) -> str | None:
    m = re.search(pattern, row, re.S)
    if not m:
        return None
    return _html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip()) or None


def parse_listing(html: str) -> list[dict]:
    """One HCA listing page (pure) → its judgments (newest-first as rendered)."""
    out: list[dict] = []
    for row in re.split(r'<div class="views-row">', html)[1:]:
        cite = _field(row, r'field--citation.*?</strong>(.*?)</div>')
        slug = au_case_slug(cite or "")
        if not slug:
            continue
        href = re.search(r'href="([^"]+)"', row)
        d = _hca_date(_field(row, r'field--hca-date-issued.*?</strong>(.*?)</div>'))
        out.append({
            "slug": slug,
            "citation": cite,
            "title": _field(row, r'field--title[^>]*>(.*?)<'),
            "coram": _field(row, r'field-hca-justices[^>]*>.*?</strong>(.*?)</div>'),
            "date": d.isoformat() if d else None,
            "url": urljoin(BASE, _html.unescape(href.group(1))) if href else None,
        })
    return out


_DOC_LINK = re.compile(r'href="([^"]+\.(?:docx|pdf))"', re.I)


def judgment_doc_urls(detail_html: str) -> list[str]:
    """The judgment file links on a detail page — DOCX preferred (cleaner than the PDF),
    absolute-ised. Both point at the unguarded ``/sites/default/files/eresources`` path."""
    urls = [urljoin(BASE, _html.unescape(u)) for u in _DOC_LINK.findall(detail_html)]
    return (sorted([u for u in urls if u.lower().endswith(".docx")])
            + [u for u in urls if u.lower().endswith(".pdf")])


class HCACaselawAdapter(BaseAdapter):
    source = "au-hca"
    min_interval = 2.0
    requires_js = False       # curl_cffi clears the WAF without a browser
    requires_proxy = False

    _MAX_PAGES_BACKFILL = 40  # ~14 pages cover 1998→present; headroom

    def __init__(self, *, path: str | None = None, http: _ChromeHTTP | None = None,
                 max_pages: int | None = None) -> None:
        self.path = Path(path) if path else None
        self._http = http or _ChromeHTTP()
        self._max_pages_cfg = max_pages

    # -- discovery -------------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.path is not None:
            yield from self._discover_saved()
            return
        cap = max_pages or self._max_pages_cfg or self._MAX_PAGES_BACKFILL
        seen: set[str] = set()
        page = 0
        while page < cap:
            status, body = self._http.get(LISTING_PAGE.format(page=page))
            if status != 200:
                return
            rows = parse_listing(body.decode("utf-8", "replace"))
            if not rows:
                return
            reached = False
            progressed = False
            for j in rows:
                if j["slug"] in seen:
                    continue
                seen.add(j["slug"])
                progressed = True
                if since and j["date"] and j["date"] <= since:
                    reached = True
                    break
                yield self._stub(j)
            if reached or not progressed:
                return
            page += 1

    def _discover_saved(self) -> Iterator[Stub]:
        files = ([self.path] if self.path.is_file()
                 else sorted(self.path.rglob("*.html")) if self.path.is_dir() else [])
        seen: set[str] = set()
        for fp in files:
            try:
                html = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for j in parse_listing(html):
                if j["slug"] not in seen:
                    seen.add(j["slug"])
                    yield self._stub(j)

    def _stub(self, j: dict) -> Stub:
        return Stub(stable_id=j["slug"], landing_url=j["url"] or BASE,
                    title=j["title"] or j["citation"], court="hca",
                    hint_date=_iso_date(j["date"]), hints=j)

    # -- fetch (detail page → DOCX/PDF → text) --------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        j = stub.hints
        cite = j.get("citation")
        text = None
        doc_url = None
        needs_fetch = True
        if stub.landing_url and stub.landing_url != BASE:
            status, body = self._http.get(stub.landing_url)
            if status == 200:
                for url in judgment_doc_urls(body.decode("utf-8", "replace")):
                    dstatus, doc = self._http.get(url)
                    if dstatus != 200 or not doc:
                        continue
                    ext = "pdf" if url.lower().endswith(".pdf") else "docx"
                    try:
                        got = extract_bytes(doc, ext=ext).text
                    except Exception:  # noqa: BLE001 — a bad file must not kill the batch
                        got = None
                    if got and got.strip():
                        text, doc_url, needs_fetch = got, url, False
                        break

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=j.get("title") or cite or stub.stable_id,
            court="hca",
            decision_date=_iso_date(j.get("date")),
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=(text or f"{cite} | {j.get('title')} | {j.get('coram')}").encode("utf-8"),
            raw_ext="txt" if text else "html",
            text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["au-caselaw", "commonwealth", "hca"],
            extra={k: v for k, v in {
                "jurisdiction": "commonwealth",
                "neutral_citation": cite,
                "coram": j.get("coram"),
                "url": stub.landing_url,
                "document_url": doc_url,
                **({"metadata_only": True, "needs_fetch": True} if needs_fetch else {}),
                "aliases": [cite.casefold()] if cite else None,
            }.items() if v not in (None, [], "")},
        )


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _hca_date(text: str | None) -> date | None:
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})", (text or "").strip())
    ym = re.search(r"(\d{4})", text or "")
    if not m or not ym or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(ym.group(1)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def _iso_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None
