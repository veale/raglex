"""AESIA's AI Act guides — the Spanish AI supervisor's implementation library.

The Agencia Española de Supervisión de la Inteligencia Artificial publishes one page,
``/es/guias``, holding a numbered series of PDF guides written in the Spanish regulatory
sandbox: two introductory, thirteen requirement-by-requirement technical guides (risk
management, human oversight, data governance, transparency, accuracy, robustness,
cybersecurity, logging, post-market monitoring, incident reporting, technical
documentation) and a manual for the checklists. They are the most detailed public
account of what a Member State expects an AI Act Chapter III obligation to look like in
practice, and the page itself says they are the basis Spain is offering the Commission's
working group for the European guidance.

## Why the default instrument matters more here than anywhere else

These guides discuss the AI Act *continuously* and name it almost never. A technical
guide opens on its subject and then writes "el artículo 9", "el anexo IV", "el
considerando 27" for forty pages. Without a declared governing instrument the
carry-forward pass has no antecedent to attach any of it to, so a document that is
entirely about Article 9 produces no edge to Article 9 at all — the exact silent
incompleteness this repository exists to avoid. Every record therefore declares the AI
Act as its ``citation_default_instrument`` — and, where a guide's own title names a
different instrument, that instrument wins for that guide, so a future "Guía sobre el
Reglamento (UE) 2016/679" does not have its bare Articles filed under the AI Act. None
of the sixteen current guides names one; the rule is there because the series is
explicitly still growing.

## The page

A flat grid of ``<a href="…/storage/media/NN-….pdf" title="NN Guía …">`` links with no
dates, no pagination and no detail pages. The number is the series position and part of
the identity: the guides are cited as "guía 05" in the checklists and in the sandbox
material. A companion ZIP of checklists sits alongside them and is deliberately not
harvested — it is a bundle of spreadsheets, not a document.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub
from ..extraction.ocr import text_or_ocr
from ._governing_instrument import AI_ACT, default_instrument

log = logging.getLogger(__name__)

BASE = "https://aesia.digital.gob.es"
LISTING = f"{BASE}/es/guias"

#: Every guide link on the page, found by splitting on the OPENING anchor rather than
#: matching a closing one — the rule in ``docs/adapter-authoring.md``. There is no
#: terminator that works for the last card in the grid, and the whole page is sixteen
#: identically-nested cards, so a closing match would silently drop the checklist manual.
_ANCHOR = re.compile(
    r'<a\s[^>]*href="(?P<url>[^"]*/storage/[^"]*\.pdf)"[^>]*?'
    r'title="(?P<title>[^"]*)"', re.IGNORECASE)
#: …and the same anchor with the attributes the other way round, which the template also
#: emits. Both orders are collected and deduplicated on the URL.
_ANCHOR_REVERSED = re.compile(
    r'<a\s[^>]*title="(?P<title>[^"]*)"[^>]*?'
    r'href="(?P<url>[^"]*/storage/[^"]*\.pdf)"', re.IGNORECASE)
#: "01 Guía introductoria al reglamento de IA" — the leading number is the series
#: position, kept as metadata and stripped from the display title.
_NUMBERED_TITLE = re.compile(r"^\s*(?P<number>\d{1,3})\s+(?P<rest>.+)$")


def _clean(value: str | None) -> str:
    return " ".join(html.unescape(value or "").split())


def guide_slug(url: str) -> str:
    """``…/05-guia-de-gestion-de-riesgos.pdf`` → ``05-guia-de-gestion-de-riesgos``.

    The filename carries the series number, so it is a stable, readable and *ordered*
    key. AESIA revises these guides in place under the same filename — the page says so
    — which is exactly what the payload hash is for: a revision becomes a new version of
    one document rather than a second document.
    """
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "guia"


def listing_items(html_text: bytes | str) -> list[dict]:
    """One dict per guide PDF, in page order."""
    if isinstance(html_text, bytes):
        html_text = html_text.decode("utf-8", "replace")
    found: dict[str, dict] = {}
    for pattern in (_ANCHOR, _ANCHOR_REVERSED):
        for match in pattern.finditer(html_text):
            url = urljoin(BASE, _clean(match.group("url")))
            if url in found:
                continue
            title = _clean(match.group("title"))
            number = None
            numbered = _NUMBERED_TITLE.match(title)
            if numbered:
                number, title = numbered.group("number"), _clean(numbered.group("rest"))
            found[url] = {"url": url, "title": title, "series_number": number,
                          "position": len(found)}
    return list(found.values())


class AESIAGuidesAdapter(BaseAdapter):
    source = "es-aesia-guias"
    min_interval = 1.0

    def __init__(
        self, *, ocr: bool | str | None = None,
        max_ocr_pages: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.ocr = option_flag(ocr, True)
        self.max_ocr_pages = max(0, option_int(max_ocr_pages, 300))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # One page, no pagination and no dates, so ``since`` cannot narrow anything and
        # there is no cursor to resume from: the whole grid is read every run and the
        # payload hash decides what changed (INCREMENTAL_MODE "full-walk").
        rows = listing_items(self._client.get(LISTING).content)
        if not rows:
            # An empty grid means the page was redesigned, not that AESIA withdrew every
            # guide. Raising keeps "the parser broke" from looking like "there is
            # nothing there" — the distinction §3 of AGENTS.md is about.
            raise ValueError(f"{LISTING} yielded no guide PDFs; the listing markup changed")
        for row in rows:
            yield Stub(
                stable_id=f"es/aesia/{guide_slug(row['url'])}",
                landing_url=LISTING, raw_url=row["url"], title=row["title"],
                court="ai-supervisor-es",
                hints={"series_number": row["series_number"],
                       "feed_total": len(rows), "position": row["position"]},
            )

    def fetch(self, stub: Stub) -> Record | None:
        try:
            pdf = self._client.get(stub.raw_url).content
        except FetchError:
            return None
        if not pdf.startswith(b"%PDF"):
            log.warning("%s: not a PDF: %s", self.source, stub.raw_url)
            return None
        text, needs_ocr, spans, engine = text_or_ocr(
            pdf, max_pages=self.max_ocr_pages if self.ocr else 0)
        if len((text or "").strip()) < 200:
            needs_ocr = True
        segments = [Segment(label=f"p. {number}", char_start=start, char_end=end,
                            kind="page") for number, start, end in spans]
        number = stub.hints.get("series_number")
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.GUIDANCE,
            title=stub.title, court="ai-supervisor-es",
            language="es", source_language="es",
            landing_url=stub.landing_url, raw_bytes=pdf, raw_ext="pdf",
            text=text or None, segments=segments, extracted_via=ExtractedVia.SCRAPE,
            topic_tags=["artificial-intelligence", "ai-act", "spain", "aesia"],
            extra={
                "jurisdiction": "es",
                "issuer": "Agencia Española de Supervisión de la Inteligencia Artificial",
                "series_number": number,
                "pdf_url": stub.raw_url,
                "format": engine, "needs_ocr": needs_ocr or None,
                # The whole point of this source: a guide that says "el artículo 9" for
                # forty pages without naming the Regulation again still links to
                # Article 9 of the AI Act. The title overrides it where the guide is
                # about something else.
                "citation_default_instrument": default_instrument(stub.title, AI_ACT),
                # These guides explain the Regulation in plain language and often go a
                # page at a time without a formal citation; gating on one would drop
                # exactly the practical material they exist for.
                "require_recognized_legal_citation": False,
            },
        )
