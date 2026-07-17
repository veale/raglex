"""Ofcom enforcement actions adapter (§1.9/§4a).

Ofcom's enforcement register — investigations, provisional/confirmation decisions and
penalties. The listing is a server-rendered search over a topic
(``/enforcement?SelectedTopic=67866`` is online safety, i.e. the Online Safety Act
2023). Each result is an HTML **action page** that narrates the case and links its
documents (decision notices, penalty notices, and referenced guidance/codes).

One enforcement action ⇒ **one record**: the action's HTML narrative combined with the
text of its case-specific PDFs (the decision/penalty/notice documents), so the whole
action is searchable as a unit. The PDFs are **classified** (they are linked in varying
ways across pages) into:

  - *case documents* — decision / provisional decision / confirmation decision / penalty
    notice / information notice: these ARE the action, so their text is inlined; and
  - *referenced guidance* — Codes of Practice, guidance, research reports: these are
    separate documents (many already held under ``ofcom-osa``), so they are recorded as
    ``mentions`` edges (resolving to the held guidance via its shared slug) rather than
    duplicated into the record.

**Linking to UK law, from HTML and PDF.** The combined text is mined by the §5b
extractor, so every "section 12 of the Online Safety Act 2023" (and any other named UK
Act) links to its provision — now that the OSA is in the statute map. The action's own
title usually names the section ("…under section 12"), which also mints a pinpoint
``interprets`` edge to the OSA. Enforcement is domestic, so it is NOT under the
EU-guidance name guard: it links national legislation freely.

**Updates.** An enforcement action grows in place — a new dated section, a decision
PDF, a status change (Open → Closed). ``discover`` fetches each action page and hashes
the load-bearing content (status + the document set with version tokens + a digest of
the narrative); a changed hash re-fetches the action, surfacing the new material.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterator
from urllib.parse import urlencode

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
from .ofcom import _slug as _guidance_slug  # shared PDF→ofcom-osa slug, for cross-links

BASE_URL = "https://www.ofcom.org.uk"
OSA_ID = "ukpga/2023/50"
# Topic → the primary UK statute that topic's enforcement is under. Online safety
# (67866) is the Online Safety Act 2023; other topics can be added.
TOPIC_REGIME = {"67866": OSA_ID}

_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) "
                           "Gecko/20100101 Firefox/152.0")}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def _clean(fragment: str) -> str:
    import html as _html

    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _parse_date(text: str) -> date | None:
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+((?:19|20)\d{2})\b", text or "")
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


# ── listing ──────────────────────────────────────────────────────────────
# Parse card-by-card (split on the result-block marker) rather than one regex spanning
# cards: a single card missing a summary/date paragraph must not desync the match and
# drop the cards after it. Only the link + title are required.
_CARD_URL = re.compile(r'<a[^>]+href="(?P<url>[^"]+)"')
_CARD_TITLE = re.compile(r'info-card-header"[^>]*>(?P<title>.*?)</h3>', re.S)
_CARD_DATE = re.compile(r'(?:Published|Updated):\s*(?P<date>[^<]+?)\s*</p>', re.S)


@dataclass(frozen=True, slots=True)
class ListingItem:
    url: str
    title: str
    published: date | None
    summary: str


def parse_listing(html: str) -> list[ListingItem]:
    out: list[ListingItem] = []
    seen: set[str] = set()
    for chunk in re.split(r'search-results-block', html)[1:]:
        um, tm = _CARD_URL.search(chunk), _CARD_TITLE.search(chunk)
        if not um or not tm:
            continue
        url = um.group("url")
        if url in seen or ".pdf" in url.lower():  # direct-PDF results handled by ofcom-osa
            continue
        seen.add(url)
        dm = _CARD_DATE.search(chunk)
        # the summary is the first prose <p> after the title that isn't the date line
        summary = ""
        for sm in re.finditer(r'<p[^>]*>(.*?)</p>', chunk, re.S):
            t = _clean(sm.group(1))
            if t and not re.match(r'^(Published|Updated):', t) and len(t) > 15:
                summary = t
                break
        out.append(ListingItem(url=url, title=_clean(tm.group("title")),
                               published=_parse_date(_clean(dm.group("date"))) if dm else None,
                               summary=summary))
    return out


# ── PDF classification ─────────────────────────────────────────────────────
# (kind, inline?) — case documents are inlined into the record; referenced guidance is
# linked, not duplicated. Ordered: first match wins (most specific first).
_PDF_RULES: tuple[tuple[str, str, bool], ...] = (
    (r"penalty\s+notice|financial\s+penalty", "penalty", True),
    (r"confirmation\s+decision", "confirmation-decision", True),
    (r"provisional\s+decision|provisional\s+notice", "provisional-decision", True),
    (r"\bdecision\b", "decision", True),
    (r"information\s+notice|provisional\s+notice|notice\b", "notice", True),
    (r"code\s+of\s+practice|codes\s+of\s+practice", "code", False),
    (r"guidance", "guidance", False),
    (r"research|report|register\s+of\s+risks", "report", False),
)


def classify_pdf(label: str, url: str) -> tuple[str, bool]:
    """(kind, inline) for a PDF from its anchor label + url. Unknown → 'document',
    inlined (a case page's own attachment is usually case material)."""
    hay = f"{label} {url}".lower()
    for pat, kind, inline in _PDF_RULES:
        if re.search(pat, hay):
            return kind, inline
    return "document", True


_PDF_LINK = re.compile(r'<a[^>]+href="(?P<href>[^"]+?\.pdf)(?:\?v=(?P<v>\d+))?"[^>]*>(?P<label>.*?)</a>', re.S)
_STATUS = re.compile(r'>\s*(Open|Closed|Concluded|Ongoing|Suspended)\s*<')
# section/part of the OSA named in the title/text (bare — the online-safety topic's
# instrument is the OSA, so a bare "section 12" pinpoints it).
_SECTION = re.compile(r"\b(?:section|s\.)\s*(\d+[A-Za-z]?)\b", re.IGNORECASE)
_PART = re.compile(r"\bPart\s+(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Pdf:
    url: str          # absolute
    version: str | None
    label: str
    kind: str
    inline: bool


@dataclass(frozen=True, slots=True)
class Detail:
    title: str
    status: str | None
    narrative: str
    pdfs: tuple[Pdf, ...]
    published: date | None


def parse_detail(html: str) -> Detail:
    """The action page: H1 title, status, the narrative paragraphs, and every linked
    PDF classified. Pure."""
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    body = re.split(r"<footer|Follow us</|About Ofcom</", html, maxsplit=1)[0]
    paras = [_clean(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", body, re.S)]
    narrative = "\n".join(p for p in paras if len(p) > 25)
    st = _STATUS.search(html)
    pdfs, seen = [], set()
    for m in _PDF_LINK.finditer(body):
        href = m.group("href")
        if href in seen:
            continue
        seen.add(href)
        label = _clean(m.group("label"))
        kind, inline = classify_pdf(label, href)
        pdfs.append(Pdf(url=href if href.startswith("http") else BASE_URL + href,
                        version=m.group("v"), label=label, kind=kind, inline=inline))
    return Detail(title=_clean(h1.group(1)) if h1 else "", status=st.group(1) if st else None,
                  narrative=narrative, pdfs=tuple(pdfs), published=_parse_date(narrative))


def _content_hash(d: Detail) -> str:
    payload = "|".join([
        d.status or "",
        *sorted(f"{p.url.split('?')[0]}@{p.version or ''}" for p in d.pdfs),
        hashlib.sha256(d.narrative.encode()).hexdigest()[:16],
    ])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _action_slug(url: str) -> str:
    seg = [p for p in url.split("?")[0].split("/") if p][-1]
    return "ofcom-enf/" + re.sub(r"[^a-z0-9]+", "-", seg.lower()).strip("-")


class OfcomEnforcementAdapter(BaseAdapter):
    source = "ofcom-enforcement"
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    def __init__(self, *, topic: str = "67866", results: int = 200,
                 client: RateLimitedClient | None = None) -> None:
        self.topic = str(topic)
        self.results = results
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def _listing_url(self) -> str:
        q = urlencode({"query": "", "SelectedTopic": self.topic, "IncludePDF": "true",
                       "SortBy": "Newest", "NumberOfResults": self.results})
        return f"{BASE_URL}/enforcement?{q}"

    def _get(self, url: str):
        return self._client.get(url, headers=_HEADERS)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        resp = self._get(self._listing_url())
        html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else str(resp.content)
        for item in parse_listing(html):
            # fetch the action page to hash its content (status + doc set + narrative) —
            # the reliable update signal; passed on so fetch() need not re-fetch the HTML.
            # A single bad action page (404, transient error, unparseable) must NOT abort
            # the whole crawl — skip it and carry on with the rest.
            url = item.url if item.url.startswith("http") else BASE_URL + item.url
            try:
                dresp = self._get(url)
                dhtml = dresp.content.decode("utf-8", "replace") if isinstance(dresp.content, bytes) else str(dresp.content)
                detail = parse_detail(dhtml)
            except Exception:  # noqa: BLE001 — one action shouldn't sink the register
                continue
            yield Stub(
                stable_id=_action_slug(item.url),
                landing_url=item.url if item.url.startswith("http") else BASE_URL + item.url,
                raw_url=item.url,
                hint_date=item.published or detail.published,
                title=detail.title or item.title,
                hints={"item": item, "detail": detail, "contenthash": _content_hash(detail)},
            )

    def fetch(self, stub: Stub) -> Record | None:
        from ..extraction import extract_bytes

        item: ListingItem = stub.hints["item"]
        detail: Detail = stub.hints["detail"]
        regime = TOPIC_REGIME.get(self.topic, OSA_ID)

        # combined text: the HTML narrative + the inlined case-document PDFs
        parts = [detail.title, "", f"Status: {detail.status}" if detail.status else "",
                 item.summary, "", detail.narrative]
        needs_ocr = False
        for p in detail.pdfs:
            if not p.inline:
                continue
            try:
                raw = self._get(p.url).content
            except Exception:  # noqa: BLE001 — a missing case PDF must not lose the action
                continue
            extracted = extract_bytes(raw, ext="pdf", mime="application/pdf")
            ptext = extracted.text
            if not (ptext and ptext.strip()):
                from .edpb import ocr_pdf
                ocr = ocr_pdf(raw)
                ptext, needs_ocr = (ocr, needs_ocr) if ocr else ("", True)
            if ptext:
                parts += ["", f"── {p.label} ({p.kind}) ──", ptext]
        text = "\n".join(x for x in parts if x)

        relations: list[TypedRelation] = []
        seen: set[tuple] = set()

        def _add(rt, raw, dst, anchor=None):
            key = (rt, dst, anchor)
            if dst and key not in seen:
                seen.add(key)
                relations.append(TypedRelation(relationship_type=rt, raw_citation_string=raw,
                                               dst_id=dst, dst_anchor=anchor,
                                               extracted_via=ExtractedVia.STRUCTURED,
                                               resolution_status=ResolutionStatus.PENDING))

        # Only assert an OSA link when the action is ACTUALLY under it — Ofcom's
        # enforcement register spans many regimes (broadcasting, telecoms, …) and older
        # actions predate the 2023 Act. We treat it as OSA only when the Act is named in
        # the action's own text; everything else (incl. the specific sections) is linked
        # by the §5b extractor from whatever Act the combined HTML+PDF text actually
        # names. A bare "section N" in a title is only pinned to the OSA once we know the
        # regime is the OSA.
        title_hay = f"{detail.title} {item.summary}"
        osa_named = "online safety act" in f"{title_hay} {detail.narrative}".lower()
        regime_for_extra = regime if osa_named else None
        if osa_named:
            _add(RelationshipType.INTERPRETS, "Online Safety Act 2023", regime)  # base link
            for sec in _SECTION.findall(title_hay):
                _add(RelationshipType.INTERPRETS, f"section {sec} of the Online Safety Act 2023",
                     regime, f"s. {sec}")
            for part in _PART.findall(title_hay):
                _add(RelationshipType.INTERPRETS, f"Part {part} of the Online Safety Act 2023",
                     regime, f"Part {part}")
        # referenced guidance PDFs → mentions edges that resolve to the held ofcom-osa docs
        for p in detail.pdfs:
            if not p.inline:
                _add(RelationshipType.MENTIONS, p.label or p.url,
                     _guidance_slug("/" + p.url.split("/", 3)[-1]))

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=detail.title or item.title,
            court="Ofcom",
            decision_date=stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=text.encode("utf-8"),
            raw_ext="txt",
            text=text or None,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["ofcom", "enforcement", "online-safety",
                        (detail.status or "").lower()],
            extra={k: v for k, v in {
                "issuer": "ofcom",
                "regime": regime_for_extra,
                "status": detail.status,
                "summary": item.summary,
                "topic": self.topic,
                "published": (stub.hint_date.isoformat() if stub.hint_date else None),
                "documents": [{"kind": p.kind, "label": p.label, "url": p.url,
                               "version": p.version, "inlined": p.inline} for p in detail.pdfs],
                "url": stub.landing_url,
                "contenthash": stub.hints.get("contenthash"),
                **({"needs_ocr": True} if needs_ocr else {}),
            }.items() if v not in (None, [], "")},
        )
