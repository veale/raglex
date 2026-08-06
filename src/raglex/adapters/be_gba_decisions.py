"""Belgian DPA (GBA/APD) Dispute Chamber decisions — the litigation chamber's own rulings.

The Gegevensbeschermingsautoriteit's *Geschillenkamer* (Chambre Contentieuse) is the
adjudicating organ of the Belgian data-protection authority: it hears complaints, finds
infringements, and imposes reprimands, orders and administrative fines. Its substantive
rulings are ``Beslissing ten gronde nr. N/YYYY``; the same docket also carries
``Schikkingsbeslissing`` (settlement decisions), which the register files under the same
filter and which are recorded here with their own decision kind rather than being dropped.

This is deliberately a separate source from ``be-gba``, which is the authority's
*guidance* register (recommendations, adviezen, documentation). A Dispute Chamber ruling
is a regulator determination — ``administrative`` per the adapter contract, not guidance
and not case law — and mixing the two would make the enforcement record unfindable inside
a pile of explanatory material.

The register is a Solr-backed search view. Each result links **straight to the PDF**;
there is no HTML landing page holding the text, so the PDF is the document. The view
paginates with ``p`` (0-indexed) over ``l`` results and prints its own total
("Resultaten van 1 tot 50 op 294"), which makes the crawl bounded and its progress
determinate rather than "walk until a page comes back empty".
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..extraction import extract_bytes

BASE = "https://www.gegevensbeschermingsautoriteit.be"
SEARCH = f"{BASE}/burger/zoeken"
# The register's own filter for the Dispute Chamber's substantive decisions. Kept as the
# literal query the site uses, because these are Solr taxonomy ids: guessing at a tidier
# spelling silently returns the whole publication register instead.
SEARCH_QUERY = {
    "q": "",
    "search_category[]": "taxonomy:publications",
    "search_type[]": "decision",
    "search_subtype[]": "taxonomy:dispute_chamber_substance_decisions",
    "s": "recent",
}
PAGE_SIZE = 50

_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11,
    "december": 12,
}

# "Beslissing ten gronde nr. 102/2026 van 12 mei 2026" — the number is the citable
# identity (Belgian practitioners cite the decision by number and year, not by URL), and
# the date sits in the same string because the register puts it there.
_TITLE_RE = re.compile(
    r"(?P<kind>Beslissing\s+ten\s+gronde|Schikkingsbeslissing|Beslissing)"
    r"\s*(?:nr\.?|n°)\s*(?P<number>\d+)\s*/\s*(?P<year>(?:19|20)\d{2})"
    r"(?:\s*van\s+(?P<day>\d{1,2})\s+(?P<month>[a-zA-Zà-ž]+)\s+(?P<dyear>(?:19|20)\d{2}))?",
    re.I,
)
_TOTAL_RE = re.compile(r"op\s+([\d.\s]+)\s*$|van\s+\d+\s+tot\s+\d+\s+op\s+([\d.\s]+)", re.I)

DECISION_KINDS = {
    "beslissing ten gronde": "substance",
    "schikkingsbeslissing": "settlement",
    "beslissing": "decision",
}


def parse_dutch_date(value: str | None) -> date | None:
    """``12 mei 2026`` → a date. Belgian Dutch names its months exactly as NL does."""
    match = re.search(r"\b(\d{1,2})\s+([a-zA-Zà-ž]+)\s+((?:19|20)\d{2})\b", value or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None


# The Dutch-language listing titles EVERY decision in Dutch, but links to the PDF in the
# language of the procedure — decision 102/2026 is titled "Beslissing ten gronde nr.
# 102/2026" and is a French document ("Chambre Contentieuse — Décision quant au fond").
# Taking the listing's language as the document's would have labelled roughly half the
# register wrong and pointed the wrong grammar at it, which is why this reads the text.
_FR_MARKERS = ("Chambre Contentieuse", "Décision quant au fond", "la Chambre",
               "du RGPD", "considérant", "Autorité de protection des données")
_NL_MARKERS = ("Geschillenkamer", "Beslissing ten gronde", "de Geschillenkamer",
               "de AVG", "overwegende", "Gegevensbeschermingsautoriteit")


def detect_language(text: str) -> str:
    """``fr`` or ``nl`` from the decision's own words, not from the listing's."""
    head = text[:8000]
    fr = sum(head.count(m) for m in _FR_MARKERS)
    nl = sum(head.count(m) for m in _NL_MARKERS)
    return "fr" if fr > nl else "nl"


def decision_id(number: str, year: str) -> str:
    """``be/gba/geschillenkamer/102-2026`` — keyed on the number the decision is cited
    by, not on the PDF filename. The register has spelt the same decision's file
    ``beslissing-ten-gronde-nr.-102-2026.pdf`` and ``...nr-102-2026.pdf``; keying on the
    filename would hold one ruling twice."""
    return f"be/gba/geschillenkamer/{int(number)}-{year}"


def result_total(html: bytes | str) -> int | None:
    """The view's own result count, so the crawl knows its end before it starts."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=re.compile(r"\bop\s+[\d.\s]+\s*$", re.I)):
        match = _TOTAL_RE.search(" ".join(str(node).split()))
        if match:
            digits = re.sub(r"[^\d]", "", match.group(1) or match.group(2) or "")
            if digits:
                return int(digits)
    return None


def decision_stubs(html: bytes | str) -> list[Stub]:
    """One stub per result card. The card's link IS the PDF."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Stub] = []
    seen: set[str] = set()
    for card in soup.select(".media"):
        link = card.select_one(".media-title a[href]")
        if not link:
            continue
        href = str(link.get("href") or "").split("#", 1)[0]
        if not href.lower().endswith(".pdf"):
            continue
        title = " ".join(link.get_text(" ", strip=True).split())
        match = _TITLE_RE.search(title)
        if not match:
            continue
        sid = decision_id(match.group("number"), match.group("year"))
        if sid in seen:
            continue
        seen.add(sid)
        url = urljoin(BASE, href)
        decided = parse_dutch_date(
            f"{match.group('day')} {match.group('month')} {match.group('dyear')}"
            if match.group("day") else None)
        summary_node = card.select_one(".media-description")
        summary = " ".join(summary_node.get_text(" ", strip=True).split()) if summary_node else ""
        # "[originele versie - in afwachting van vertaling]" is a translation-status
        # marker the register prefixes to the abstract, not part of the abstract.
        summary = re.sub(r"^\[[^\]]*\]\s*", "", summary)
        out.append(Stub(
            stable_id=sid, landing_url=url, raw_url=url, title=title, court="dpa-be",
            hint_date=decided,
            hints={
                "decision_number": f"{int(match.group('number'))}/{match.group('year')}",
                "decision_kind": DECISION_KINDS.get(
                    " ".join(match.group("kind").split()).casefold(), "decision"),
                "summary": summary or None,
                "watermark": decided.isoformat() if decided else None,
            },
        ))
    return out


class GBADecisionsAdapter(BaseAdapter):
    source = "be-gba-decisions"
    min_interval = 1.5

    def __init__(self, *, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    # ---- discovery -------------------------------------------------------------

    def _page(self, page: int):
        return self._client.get(SEARCH, params={**SEARCH_QUERY, "l": PAGE_SIZE, "p": page})

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Newest-first, so an incremental run stops at the cursor rather than walking
        the whole register. The view is sorted ``s=recent`` and the ids carry the year,
        so "already held" is reached within a page or two of a daily poll."""
        first = self._page(0)
        total = result_total(first.content)
        end = None if total is None else max(0, (total - 1) // PAGE_SIZE)
        if max_pages is not None:
            end = max_pages - 1 if end is None else min(end, max_pages - 1)
        seen: set[str] = set()
        page, html = 0, first.content
        while True:
            rows = [s for s in decision_stubs(html) if s.stable_id not in seen]
            if not rows and page:
                return
            for stub in rows:
                seen.add(stub.stable_id)
                if total is not None:
                    stub.hints["feed_total"] = total
                stub.hints["resume_offset"] = page * PAGE_SIZE
                yield stub
            page += 1
            if end is not None and page > end:
                return
            try:
                html = self._page(page).content
            except FetchError:
                return

    # ---- fetch -----------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        try:
            response = self._client.get(stub.raw_url)
        except FetchError:
            return None
        blob = response.content
        if not blob.startswith(b"%PDF"):
            return None
        try:
            extracted = extract_bytes(blob, ext="pdf", mime="application/pdf")
        except ValueError:
            return None
        text = (extracted.text or "").strip()
        if len(text) < 200:          # a scanned cover sheet is not a decision
            return None
        language = detect_language(text)
        kind = str(stub.hints.get("decision_kind") or "decision")
        number = str(stub.hints.get("decision_number") or "")
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            # A Dispute Chamber ruling is a regulator determination: administrative,
            # per the adapter contract, and deliberately not caselaw or guidance.
            doc_type=DocType.DECISION,
            title=stub.title,
            court="dpa-be",
            decision_date=stub.hint_date,
            language=language, source_language=language,
            landing_url=stub.landing_url,
            raw_bytes=blob, raw_ext="pdf", text=text,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["data-protection", "belgium", "enforcement", f"gba-{kind}",
                        f"lang-{language}"],
            extra={
                "jurisdiction": "be",
                "gba_decision_number": number or None,
                "gba_decision_kind": kind,
                "gba_summary": stub.hints.get("summary"),
                "gba_chamber": "geschillenkamer",
                # Every Dispute Chamber decision is grounded in the GDPR, so an orphaned
                # "artikel 6.1.f)" later in the text returns to the Regulation rather
                # than to whichever national act was named most recently. The Belgian
                # framework acts (WOG, Kaderwet) are cited BY NAME throughout and are
                # recognised by the Dutch grammar's Belgian entries.
                "citation_default_instrument": {"id": "32016R0679", "kind": "regulation"},
            },
        )
