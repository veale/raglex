"""Information Commissioner's Office — enforcement, audits, consultations, guidance.

The ICO publishes four bodies of material that a data-protection corpus needs, on one
Umbraco site with two discovery surfaces. This adapter is one class in four
**collections**, each registered as its own source so it can be watched, backfilled and
faceted independently:

  ``uk-ico-enforcement``   the enforcement register (enforcement notices, monetary
                           penalties, reprimands, prosecutions, undertakings…)
  ``uk-ico-audits``        audits, follow-up audits and sector overview reports
  ``uk-ico-consultations`` the ICO's responses to *other* bodies' consultations, plus
                           the ICO's own and stakeholder consultations
  ``uk-ico-guidance``      the guidance corpus and research library, from the sitemap:
                           ``/for-organisations/``, ``/for-the-public/`` and
                           ``/about-the-ico/research-reports-impact-and-evaluation/``

**Two discovery surfaces, two change signals.** The three registers are served by the
site's XHR search endpoint (``POST /api/search`` with a ``rootPageId``) — a small JSON
listing (≤ 9 pages of 25) carrying each item's ``createdDateTime``, which is the CMS
publish/revision stamp and therefore a real change signal. So discovery walks the whole
listing every run (cheap, no HTML) and only fetches the item pages whose stamp moved past
the cursor. The guidance corpus has no such listing; it comes from ``/sitemap.xml``,
which carries ``lastmod`` per URL — same contract, filtered by path prefix.

**The PDFs are the substance.** An enforcement item's HTML page is a two-paragraph
summary; the notice itself is a PDF, linked from a custom element
``<further-Reading x-href=… x-title=… x-size=…>``. The same element is also used for
*related pages* (an ICO news story, an external consultation), which carry ``x-location``
and no size — those are recorded as links, never downloaded. Attachments are downloaded,
text-extracted (OCR'd if the PDF is a scan), and inlined into the record so the whole
action is searchable as one unit. A PDF linked from several pages is fetched once per
run.

**Naming the law.** ICO material is written in acronyms — PECR, the DPA 2018, FOIA, the
EIR — and, since Brexit, "the GDPR" in an ICO document means the *UK* GDPR
(``european/regulation/2016/0679``), not CELEX 32016R0679. Every item is scanned for the
instruments the Commissioner enforces (:data:`ICO_REGIMES`); each one found becomes an
``interprets`` edge and a topic tag, and where exactly one is found the record declares
it as ``citation_default_instrument`` (so a later orphaned "regulation 21" or "Article
5(1)(f)" returns to the right instrument) and as ``statutory_basis`` (so the extractor
binds this document's "the Act" / "the Regulations"). The acronyms themselves resolve
through the source-scoped alias table in :mod:`raglex.citations.stage`, and the
GDPR→UK GDPR rebinding lives there too — both are per-document facts about ICO material,
not corpus-wide claims.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from ..core.adapter import BaseAdapter, resume_floor
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

BASE_URL = "https://ico.org.uk"
SEARCH_API = f"{BASE_URL}/api/search"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

# ico.org.uk/robots.txt declares ``Crawl-delay: 6``. That is a courtesy floor for a
# whole-site crawler; we fetch a few hundred pages a week, so the default self-limit is
# gentler than the site's own rate limiting needs and can be raised for a first backfill
# (``-o min_interval=6``).
DEFAULT_MIN_INTERVAL = 2.0

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) "
                   "Gecko/20100101 Firefox/153.0"),
    "Accept-Language": "en-GB,en;q=0.9",
}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _parse_long_date(text: str | None) -> date | None:
    """"28 May 2026" → a date. The ICO writes every visible date this way."""
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+((?:19|20)\d{2})\b", text or "")
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


# ── the instruments the Commissioner enforces ────────────────────────────────
# Two patterns per instrument, and the split matters: the spelled-out names match
# case-INSENSITIVELY, the bare acronyms UPPERCASE-only — the same discipline the
# citation grammars use, so a lower-case "pecr" in a filename can never mint an edge.
#
# ``bare_gdpr`` is the deliberate exception. In an ICO publication "the GDPR" is the
# UK GDPR: the Commissioner has no jurisdiction over the EU instrument, and every
# post-2021 notice that says "Article 5(1)(f) of the UK GDPR" says "the GDPR" for the
# next forty paragraphs. The corresponding rebinding of citations the grammars have
# already resolved to CELEX 32016R0679 lives in ``citations.stage``.
@dataclass(frozen=True, slots=True)
class ICORegime:
    id: str                 # the corpus id of the instrument
    kind: str               # "act" | "regulation" — what a pinpoint of it is called
    name: str               # the proper name, as a citation of it would be written
    tag: str                # the topic tag
    names: str              # regex alternation, matched case-insensitively
    acronyms: str = ""      # regex alternation, matched case-SENSITIVELY (uppercase)


ICO_REGIMES: tuple[ICORegime, ...] = (
    # Order matters only for readability; every regime is tested independently.
    ICORegime(
        id="european/regulation/2016/0679", kind="regulation",
        name="UK General Data Protection Regulation", tag="uk-gdpr",
        names=(r"(?:UK|United\s+Kingdom)\s+GDPR"
               r"|(?:UK|United\s+Kingdom)\s+General\s+Data\s+Protection\s+Regulation"
               r"|General\s+Data\s+Protection\s+Regulation"),
        acronyms=r"GDPR",
    ),
    ICORegime(
        id="ukpga/2018/12", kind="act", name="Data Protection Act 2018", tag="dpa-2018",
        names=r"Data\s+Protection\s+Act\s+2018", acronyms=r"DPA\s?(?:20)?18|DPA",
    ),
    ICORegime(
        id="ukpga/1998/29", kind="act", name="Data Protection Act 1998", tag="dpa-1998",
        names=r"Data\s+Protection\s+Act\s+1998", acronyms=r"DPA\s?(?:19)?98",
    ),
    ICORegime(
        id="uksi/2003/2426", kind="regulation",
        name="Privacy and Electronic Communications (EC Directive) Regulations 2003",
        tag="pecr",
        names=(r"Privacy\s+and\s+Electronic\s+Communications"
               r"(?:\s+\(EC\s+Directive\))?\s+Regulations(?:\s+2003)?"),
        acronyms=r"PECR",
    ),
    ICORegime(
        id="ukpga/2000/36", kind="act", name="Freedom of Information Act 2000",
        tag="foia", names=r"Freedom\s+of\s+Information\s+Act(?:\s+2000)?",
        acronyms=r"FOIA(?:\s?2000)?",
    ),
    ICORegime(
        id="uksi/2004/3391", kind="regulation",
        name="Environmental Information Regulations 2004", tag="eir",
        names=r"Environmental\s+Information\s+Regulations(?:\s+2004)?", acronyms=r"EIR",
    ),
    ICORegime(
        id="ukpga/2025/18", kind="act", name="Data (Use and Access) Act 2025",
        tag="duaa", names=r"Data\s+\(Use\s+and\s+Access\)\s+Act(?:\s+2025)?",
        acronyms=r"DUAA",
    ),
    ICORegime(
        id="uksi/2018/506", kind="regulation",
        name="Network and Information Systems Regulations 2018", tag="nis",
        names=r"Network\s+and\s+Information\s+Systems\s+Regulations(?:\s+2018)?",
        acronyms=r"NIS\s+Regulations",
    ),
    ICORegime(
        id="european/regulation/2014/0910", kind="regulation",
        name="eIDAS Regulation", tag="eidas",
        names=r"eIDAS(?:\s+Regulation)?|electronic\s+identification\s+and\s+trust\s+services",
    ),
    ICORegime(
        id="uksi/2009/3157", kind="regulation", name="INSPIRE Regulations 2009",
        tag="inspire", names=r"INSPIRE\s+Regulations(?:\s+2009)?",
    ),
    ICORegime(
        id="uksi/2015/1415", kind="regulation",
        name="Re-use of Public Sector Information Regulations 2015", tag="rpsi",
        names=r"Re-?use\s+of\s+Public\s+Sector\s+Information\s+Regulations(?:\s+2015)?",
    ),
    ICORegime(
        id="uksi/2018/480", kind="regulation",
        name="Data Protection (Charges and Information) Regulations 2018", tag="dp-fee",
        names=r"Data\s+Protection\s+\(Charges\s+and\s+Information\)\s+Regulations(?:\s+2018)?",
    ),
)

_REGIME_NAME_RE = {r.id: re.compile(rf"\b(?:{r.names})\b", re.IGNORECASE)
                   for r in ICO_REGIMES}
_REGIME_ACRONYM_RE = {r.id: re.compile(rf"\b(?:{r.acronyms})\b")
                      for r in ICO_REGIMES if r.acronyms}
REGIMES_BY_ID = {r.id: r for r in ICO_REGIMES}


def regimes_in(text: str | None) -> list[tuple[ICORegime, int]]:
    """The instruments this text names, with how many times, most-mentioned first.

    The DPA 1998 is a trap: "Data Protection Act 1998" contains no substring that the
    2018 patterns match, but the shared acronym "DPA" does — an old notice under the
    1998 Act would otherwise also claim the 2018 one. So a bare acronym only counts when
    no *other* regime's full name has already claimed the same family.
    """
    if not text:
        return []
    spans = {r.id: [m.span() for m in _REGIME_NAME_RE[r.id].finditer(text)]
             for r in ICO_REGIMES}
    named = {rid for rid, s in spans.items() if s}
    found: list[tuple[ICORegime, int]] = []
    for r in ICO_REGIMES:
        count = len(spans[r.id])
        if r.id in _REGIME_ACRONYM_RE:
            # "DPA" alone, in a document that has named the 1998 Act and not the 2018
            # one, is the 1998 Act — don't mint the newer Act off the shared letters.
            siblings = {x.id for x in ICO_REGIMES
                        if x.id != r.id and x.tag.split("-")[0] == r.tag.split("-")[0]}
            if count or not (siblings & named):
                # An acronym INSIDE a name already counted is the same reference:
                # "UK GDPR" is one mention, not one name plus one acronym. Counting it
                # twice doubles the leader and lets it clear the dominance bar on a
                # genuinely contested document.
                count += sum(
                    1 for m in _REGIME_ACRONYM_RE[r.id].finditer(text)
                    if not any(a <= m.start() and m.end() <= b for a, b in spans[r.id])
                )
        if count:
            found.append((r, count))
    found.sort(key=lambda rc: -rc[1])
    return found


def dominant_regime(counted: list[tuple[ICORegime, int]],
                    headline: list[tuple[ICORegime, int]] | None = None,
                    ) -> ICORegime | None:
    """The one instrument a document is *about*, or None when that is contested.

    Raw body counts alone do not decide this. An enforcement notice under PECR names the
    DPA 2018 a dozen times — that is where the Commissioner's power to issue the notice
    and the appeal route come from — so "exactly one regime named" never fires on a real
    notice, and a bare 3×-dominance test fails a 26-vs-11 PECR notice. Yet without a
    declared instrument every later "regulation 21(1)(b)" in that notice orphans.

    So the register's own framing is asked first: the title, the ICO's one-line summary
    and the opening of the page say what the action is *under* ("Contravention of
    Regulation 21A and 24 of the PECR", "…for infringing Articles 5(1)(a), 6, and 8 …
    UK GDPR"). Only if that is contested does the whole-body count decide, under the
    stricter 3× rule the guidance classifier uses. A genuinely mixed document — a
    consultation response ranging across FOIA, the EIR and the UK GDPR — declares
    nothing, which is the right answer for it.
    """
    for pool, factor in ((headline or [], 2), (counted, 3)):
        if not pool:
            continue
        (leader, n) = pool[0]
        runner = pool[1][1] if len(pool) > 1 else 0
        if not runner or n >= factor * runner:
            return leader
    return None


# ── the registers (the site's XHR search endpoint) ───────────────────────────
@dataclass(frozen=True, slots=True)
class Register:
    root_page_id: int
    path_prefix: str    # the URL subtree its items live under
    id_prefix: str      # the stable_id namespace
    doc_type: DocType
    tag: str


REGISTERS: dict[str, tuple[Register, ...]] = {
    "enforcement": (
        Register(17222, "/action-weve-taken/enforcement/", "uk-ico/enforcement",
                 DocType.DECISION, "enforcement"),
    ),
    "audits": (
        Register(17223, "/action-weve-taken/audits-and-overview-reports/",
                 "uk-ico/audit", DocType.GUIDANCE, "audit"),
    ),
    # Both consultation registers, in one source: the Commissioner's responses to other
    # bodies' consultations, and the ICO's own / stakeholder consultations. They are the
    # same kind of document and a reader looking for "what did the ICO say about X"
    # wants both, but their URL subtrees differ so their ids never collide.
    "consultations": (
        Register(67781, "/about-the-ico/consultations/", "uk-ico/consultation-response",
                 DocType.GUIDANCE, "consultation-response"),
        Register(69493, "/about-the-ico/ico-and-stakeholder-consultations/",
                 "uk-ico/consultation", DocType.GUIDANCE, "consultation"),
    ),
}


@dataclass(frozen=True, slots=True)
class ListingItem:
    url: str            # site-relative
    title: str
    created: str        # ISO8601 CMS stamp — the change signal AND the watermark
    meta: str           # "28 May 2026, Enforcement notices, Marketing"
    description: str
    register: Register


def parse_listing(payload: dict, register: Register) -> tuple[list[ListingItem], int]:
    """One page of the search API → its items and the total page count. Pure.

    Rows without a URL (the API occasionally returns a promoted card) are dropped, as
    are rows outside the register's own subtree — the endpoint is generic and a
    mis-typed ``rootPageId`` should yield nothing rather than the wrong register.
    """
    items: list[ListingItem] = []
    for row in payload.get("results") or []:
        url = (row.get("url") or "").strip()
        if not url or not url.startswith(register.path_prefix):
            continue
        items.append(ListingItem(
            url=url,
            title=" ".join((row.get("title") or "").split()),
            created=(row.get("createdDateTime") or "").strip(),
            meta=(row.get("filterItemMetaData") or "").strip(),
            description=(row.get("description") or "").strip(),
            register=register,
        ))
    total_pages = int((payload.get("pagination") or {}).get("totalPages") or 1)
    return items, total_pages


# ── the sitemap (the guidance / research corpus) ─────────────────────────────
# path prefix → (stable_id namespace, topic tag). Only these subtrees are harvested:
# the rest of the site is the enforcement registers (covered above), 26k FOI decision
# notices (a corpus of their own), press releases and recruitment.
GUIDANCE_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("/for-organisations/", "uk-ico/guidance", "guidance"),
    ("/for-the-public/", "uk-ico/public", "public-information"),
    ("/about-the-ico/research-reports-impact-and-evaluation/", "uk-ico/research",
     "research"),
)

# Pages that exist for the website's plumbing, not for their content: form landing
# pages, thank-you pages, and the campaign microsite's item pages.
_SKIP_PATHS = re.compile(
    r"/(?:thanks?|thank-you|search|jobs|speaker-request-form|"
    r"information-access-team-form|[a-z-]*-draft)/$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    url: str
    path: str
    lastmod: str | None
    id_prefix: str
    tag: str


def parse_sitemap(xml_bytes: bytes,
                  sections: tuple[tuple[str, str, str], ...] = GUIDANCE_SECTIONS,
                  ) -> list[SitemapEntry]:
    """The harvestable entries of ``/sitemap.xml``. Pure."""
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[SitemapEntry] = []
    for url in root.findall(f"{ns}url"):
        loc = (url.findtext(f"{ns}loc") or "").strip()
        if not loc:
            continue
        path = urlsplit(loc).path
        for prefix, id_prefix, tag in sections:
            if not path.startswith(prefix) or path == prefix:
                continue
            if _SKIP_PATHS.search(path):
                continue
            out.append(SitemapEntry(
                url=loc, path=path,
                lastmod=(url.findtext(f"{ns}lastmod") or "").strip() or None,
                id_prefix=id_prefix, tag=tag,
            ))
            break
    return out


# ── the item / guidance page ─────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Attachment:
    url: str            # absolute
    title: str
    size: int | None

    @property
    def ext(self) -> str:
        return (urlsplit(self.url).path.rsplit(".", 1)[-1] or "").lower()


@dataclass(frozen=True, slots=True)
class Page:
    title: str
    body: str
    # the labelled facts panel: {"date": "28 May 2026", "type": "Enforcement notices",
    # "sector": "Marketing", "start date": …, "closing date": …, "status": …}
    facts: dict[str, str] = field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()
    related: tuple[dict, ...] = ()
    page_id: str | None = None
    dc_date: date | None = None
    # the breadcrumb trail, ending with the page itself
    breadcrumb: tuple[str, ...] = ()


# The ICO publishes strategy and consultation documents in English and Welsh side by
# side ("Draft corporate strategy - Welsh / Strategaeth Gorfforaethol Ddrafft"; the file
# names carry ``-cym-``). The record declares ``language="en"``, so inlining the Welsh
# twin doubles the document's text in a language the record does not claim to be in.
_WELSH = re.compile(r"\b(?:welsh|cymraeg|cymru)\b|[-_]cym[-_.]", re.IGNORECASE)


def _attachment_nodes(soup) -> tuple[list[Attachment], list[dict]]:
    """Split the page's ``<further-Reading>`` elements into downloadable attachments and
    related links.

    The element is used for both. A file carries ``x-size`` and a same-site path; a
    related page carries ``x-location`` (the section it lives in, or "External link").
    Judging on ``x-location`` alone would miss a sizeless attachment, and on the ``.pdf``
    suffix alone would download an externally-hosted consultation paper the ICO merely
    points at — so both signals are used.
    """
    files: list[Attachment] = []
    links: list[dict] = []
    seen: set[str] = set()
    for node in soup.find_all(re.compile(r"^further-reading$", re.IGNORECASE)):
        href = (node.get("x-href") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        title = " ".join((node.get("x-title") or "").split())
        url = urljoin(BASE_URL, href)
        size = None
        try:
            size = int(node.get("x-size")) if node.get("x-size") else None
        except (TypeError, ValueError):
            size = None
        same_site = urlsplit(url).netloc.endswith("ico.org.uk")
        is_file = same_site and (size is not None
                                 or bool(re.search(r"\.(pdf|docx?|rtf|odt)$",
                                                   urlsplit(url).path, re.IGNORECASE)))
        if is_file and _WELSH.search(f"{title} {urlsplit(url).path}"):
            continue   # the Welsh twin of an English document — see _WELSH
        if is_file:
            files.append(Attachment(url=url, title=title, size=size))
        else:
            links.append({"url": url, "title": title,
                          "location": (node.get("x-location") or "").strip() or None})
    return files, links


# Site chrome that the prose extraction cannot tell from content, because it IS prose
# and it sits in the same rich-text block. The review banner is the dangerous one: it
# names the Data (Use and Access) Act on roughly a thousand guidance pages, so without
# this every one of them would be tagged ``duaa`` and — on a page that cites nothing
# else — would declare the DUAA as its governing instrument.
_BOILERPLATE = (
    re.compile(r"^Due to changes made by the Data \(Use and Access\) Act", re.I),
    re.compile(r"^Plans for new and updated guidance page$", re.I),
    re.compile(r"^will tell you about which guidance will be updated", re.I),
    re.compile(r"^Click to toggle details$", re.I),
    re.compile(r"^(?:Skip to main content|Back to top|Print this page)$", re.I),
)

_STRIP_SELECTORS = (
    "script, style, form, noscript, nav, header, footer, "
    "further-Reading, .print\\:hidden, "
    '[data-area-alias="right"], [data-content-element-type-alias="relatedDocumentsBlock"]'
)


def parse_page(html: str) -> Page:
    """One ICO page → title, the labelled facts panel, prose body, attachments and
    related links. Pure; no network."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    meta = {m.get("name", "").lower(): (m.get("content") or "").strip()
            for m in soup.find_all("meta") if m.get("name")}
    h1 = soup.find("h1")
    title = " ".join(h1.get_text(" ", strip=True).split()) if h1 else (
        meta.get("dc.title") or "")

    main = soup.select_one("main#main-content") or soup.find("main") or soup
    # attachments, the breadcrumb and the facts panel are read BEFORE the strip pass
    crumbs: list[str] = []
    crumb_nav = main.select_one('nav[aria-label="breadcrumb"]')
    if crumb_nav:
        crumbs = [" ".join(li.get_text(" ", strip=True).split())
                  for li in crumb_nav.find_all("li")]
        crumbs = [c for c in crumbs if c]
    files, links = _attachment_nodes(main)
    facts: dict[str, str] = {}
    for li in main.find_all("li"):
        label, value = li.find("span"), li.find("strong")
        if not (label and value):
            continue
        key = " ".join(label.get_text(" ", strip=True).split()).rstrip(":").lower()
        val = " ".join(value.get_text(" ", strip=True).split())
        if key and val and key not in facts and len(key) <= 30:
            facts[key] = val

    for node in main.select(_STRIP_SELECTORS):
        node.decompose()
    for node in main.find_all(re.compile(r"^further-reading$", re.IGNORECASE)):
        node.decompose()
    lines = [" ".join(line.split()) for line in main.get_text("\n").splitlines()]
    body = "\n".join(line for line in lines
                     if line and not any(p.search(line) for p in _BOILERPLATE)).strip()

    dc_date = None
    if meta.get("dc.date"):
        # "Wednesday, July 08, 2026" — the CMS's own long form
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*((?:19|20)\d{2})", meta["dc.date"])
        if m and m.group(1).lower() in _MONTHS:
            try:
                dc_date = date(int(m.group(3)), _MONTHS[m.group(1).lower()],
                               int(m.group(2)))
            except ValueError:
                dc_date = None
    return Page(title=title, body=body, facts=facts, attachments=tuple(files),
                related=tuple(links), page_id=meta.get("dc.pageid") or None,
                dc_date=dc_date, breadcrumb=tuple(crumbs))


def qualified_title(title: str, breadcrumb: tuple[str, ...]) -> str:
    """A guidance sub-page's title, qualified by the guide it belongs to.

    The ICO's guides are trees of pages whose own headings are section names —
    "Introduction", "Consent", "At a glance". Stored bare, a search result reads
    "Introduction" and a citation of it says nothing. The breadcrumb names the guide, so
    a page nested at least two levels inside its section is titled "<guide> — <page>".
    A page sitting directly under its section ("For the public / Nuisance calls") is
    already self-describing and is left alone.
    """
    crumbs = [c for c in breadcrumb if c]
    if len(crumbs) < 3 or not title:
        return title
    parent = crumbs[-2]
    if not parent or parent.lower() in title.lower() or title.lower() in parent.lower():
        return title
    return f"{parent} — {title}"


# ── action types ─────────────────────────────────────────────────────────────
# The register's own "Type" vocabulary → a stable slug. An item may carry several
# ("Reprimands, Monetary penalties"), so the value is split on commas and each part
# mapped; an unrecognised type is slugified rather than dropped, so a new ICO category
# still lands somewhere findable.
_ACTION_TYPES: tuple[tuple[str, str], ...] = (
    (r"enforcement\s+notice", "enforcement-notice"),
    (r"monetary\s+penalt", "monetary-penalty"),
    (r"reprimand", "reprimand"),
    (r"prosecution", "prosecution"),
    (r"undertaking", "undertaking"),
    (r"assessment\s+notice", "assessment-notice"),
    (r"information\s+notice", "information-notice"),
    (r"decision\s+notice", "decision-notice"),
    (r"practice\s+recommendation", "practice-recommendation"),
    (r"follow-?up\s+audit", "follow-up-audit"),
    (r"overview\s+report", "overview-report"),
    (r"advisory\s+visit", "advisory-visit"),
    (r"\baudit\b", "audit"),
    (r"response\s+to\s+others", "consultation-response"),
    (r"ico\s+consultation", "ico-consultation"),
    (r"stakeholder", "stakeholder-consultation"),
    (r"call\s+for\s+(?:evidence|views)", "call-for-evidence"),
)


def action_types(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in re.split(r"\s*,\s*", raw or ""):
        part = part.strip()
        if not part:
            continue
        slug = next((s for pat, s in _ACTION_TYPES if re.search(pat, part, re.I)), None)
        slug = slug or _slug(part)
        if slug and slug not in out:
            out.append(slug)
    return out


def _item_slug(register: Register, url: str) -> str:
    """``/action-weve-taken/enforcement/2026/05/thermotech-…-en/`` →
    ``uk-ico/enforcement/2026/thermotech-…-en``.

    The trailing ``-en`` / ``-mpn`` the ICO appends is load-bearing: one company on one
    date can hold both an enforcement notice and a monetary penalty, published as two
    items, and they must be two documents.
    """
    parts = [p for p in urlsplit(url).path.split("/") if p]
    tail = parts[-1] if parts else _slug(url)
    year = next((p for p in parts if re.fullmatch(r"(?:19|20)\d{2}", p)), None)
    return f"{register.id_prefix}/{year}/{tail}" if year else f"{register.id_prefix}/{tail}"


def _guidance_slug(entry: SitemapEntry, sections=GUIDANCE_SECTIONS) -> str:
    """The path below the section prefix, kept whole — ICO guidance is a tree of short
    slugs ("consent", "what-is-personal-data") that are only unique in context."""
    prefix = next((p for p, ip, _ in sections
                   if ip == entry.id_prefix and entry.path.startswith(p)), "")
    tail = entry.path[len(prefix):].strip("/") if prefix else entry.path.strip("/")
    return f"{entry.id_prefix}/{tail}" if tail else entry.id_prefix


class ICOAdapter(BaseAdapter):
    """One collection of ICO material. ``collection`` selects which (see
    :data:`REGISTERS` and :data:`GUIDANCE_SECTIONS`)."""

    source = "uk-ico-enforcement"
    min_interval = DEFAULT_MIN_INTERVAL
    requires_js = False
    requires_proxy = False

    #: how many attachment bytes to inline per record before we stop downloading — a
    #: research library holds a few 100MB appendix packs, and one of them must not
    #: become one document's text.
    MAX_ATTACHMENT_BYTES = 40_000_000

    def __init__(self, *, collection: str = "enforcement",
                 min_interval: float | None = None,
                 sections: str | None = None,
                 start_offset: int | str | None = None,
                 client: RateLimitedClient | None = None) -> None:
        self.collection = (collection or "enforcement").strip().lower()
        if self.collection not in REGISTERS and self.collection != "guidance":
            raise ValueError(
                f"unknown ICO collection {collection!r} — "
                f"one of {', '.join([*REGISTERS, 'guidance'])}")
        self.source = f"uk-ico-{self.collection}"
        self.registers = REGISTERS.get(self.collection, ())
        # optional csv filter on the guidance subtrees ("for-organisations,for-the-public")
        wanted = {s.strip().strip("/").lower() for s in (sections or "").split(",") if s.strip()}
        self.sections = tuple(
            s for s in GUIDANCE_SECTIONS if not wanted or s[0].strip("/").lower() in wanted
        ) or GUIDANCE_SECTIONS
        # Handed back by ``jobs`` from an interrupted run's checkpoint. An adapter
        # that reports ``resume_offset`` and cannot take it back raises TypeError
        # on resume, and the retry is filed as done — see core.adapter.resume_floor.
        self.start_offset = resume_floor(start_offset, 50)
        if min_interval is not None:
            self.min_interval = float(min_interval)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)
        # per-run attachment cache: a PDF linked from several guidance pages is
        # downloaded and extracted once (url → extracted text, "" for a failure)
        self._attachment_text: dict[str, str] = {}

    # -- HTTP -----------------------------------------------------------------
    def _get(self, url: str):
        return self._client.get(url, headers=_HEADERS)

    def _search(self, register: Register, page: int) -> dict:
        resp = self._client.request(
            "POST", SEARCH_API,
            json={"filters": [], "pageNumber": page, "order": "newest",
                  "rootPageId": register.root_page_id},
            headers={**_HEADERS, "Content-Type": "application/json",
                     "Origin": BASE_URL, "Referer": BASE_URL + register.path_prefix},
        )
        raw = resp.content
        try:
            return json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes)
                              else str(raw))
        except ValueError:
            return {}

    # -- discovery -------------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.collection == "guidance":
            yield from self._discover_sitemap(since)
        else:
            yield from self._discover_registers(since, max_pages=max_pages)

    def _discover_registers(self, since: str | None, *, max_pages: int | None = None
                            ) -> Iterator[Stub]:
        """Walk each register's whole JSON listing, then yield the items whose CMS stamp
        moved past the cursor, oldest first.

        The listing is ordered by the item's own DATE, not by its publish stamp — an
        item dated May 2026 can be published in July — so there is nothing to early-stop
        on. That is affordable because the listing is JSON: nine requests cover the
        whole enforcement register, and nothing else is fetched until an item is known
        to be new or changed. Yielding oldest-first makes the cursor a resumable drip.
        """
        items: list[ListingItem] = []
        for register in self.registers:
            page, total = 1, 1
            while page <= total:
                payload = self._search(register, page)
                batch, total = parse_listing(payload, register)
                if not batch:
                    break
                items.extend(batch)
                if max_pages is not None and page >= max_pages:
                    break
                page += 1
        fresh = [i for i in items if not since or (i.created and i.created > since)]
        fresh.sort(key=lambda i: i.created)
        for n, item in enumerate(fresh):
            if n < self.start_offset:
                continue
            yield Stub(
                stable_id=_item_slug(item.register, item.url),
                landing_url=urljoin(BASE_URL, item.url),
                raw_url=item.url,
                hint_date=_parse_long_date(item.meta),
                title=item.title,
                court="ICO",
                hints={"item": item, "register": item.register,
                       "watermark": item.created, "contenthash": item.created,
                       "feed_total": len(fresh), "resume_offset": n},
            )

    def _discover_sitemap(self, since: str | None) -> Iterator[Stub]:
        """``/sitemap.xml`` filtered to the guidance/research subtrees, oldest ``lastmod``
        first. One request covers the whole site; ``lastmod`` is both the cursor and the
        change signal, so an unchanged page is never downloaded again."""
        resp = self._get(SITEMAP_URL)
        entries = parse_sitemap(resp.content, self.sections)
        if since:
            entries = [e for e in entries if e.lastmod and e.lastmod > since]
        entries.sort(key=lambda e: e.lastmod or "")
        for n, entry in enumerate(entries):
            if n < self.start_offset:
                continue
            yield Stub(
                stable_id=_guidance_slug(entry),
                landing_url=entry.url,
                raw_url=entry.url,
                hint_date=None,
                court="ICO",
                hints={"entry": entry,
                       **({"watermark": entry.lastmod, "contenthash": entry.lastmod}
                          if entry.lastmod else {}),
                       "feed_total": len(entries), "resume_offset": n},
            )

    # -- fetch ------------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        try:
            resp = self._get(stub.landing_url)
        except FetchError:
            return None            # a withdrawn page — RateLimitError still propagates
        html = (resp.content.decode("utf-8", "replace")
                if isinstance(resp.content, bytes) else str(resp.content))
        page = parse_page(html)
        item: ListingItem | None = stub.hints.get("item")
        entry: SitemapEntry | None = stub.hints.get("entry")
        register: Register | None = stub.hints.get("register")

        # A register item's title is a party name and stands alone; a guidance sub-page's
        # is a section heading and needs the guide above it.
        title = (qualified_title(page.title, page.breadcrumb) if entry is not None
                 else page.title) or stub.title or stub.stable_id
        parts = [title, ""]
        if item and item.description:
            parts += [item.description, ""]
        parts.append(page.body)
        attachments, needs_ocr = [], False
        inlined_bytes = 0
        for att in page.attachments:
            att_text, ocr_missing, nbytes = self._attachment(att, budget=(
                self.MAX_ATTACHMENT_BYTES - inlined_bytes))
            needs_ocr = needs_ocr or ocr_missing
            inlined_bytes += nbytes
            attachments.append({"url": att.url, "title": att.title or None,
                                "bytes": att.size, "text_chars": len(att_text)})
            if att_text:
                parts += ["", f"── {att.title or att.url.rsplit('/', 1)[-1]} ──", att_text]
        text = "\n".join(p for p in parts if p).strip()

        # a plumbing page with neither prose nor an attachment carries nothing to index
        if len(text) < 200 and not any(a["text_chars"] for a in attachments):
            return None

        types = action_types(page.facts.get("type") or
                             (item.meta.split(",", 1)[-1] if item else ""))
        sector = page.facts.get("sector")
        decided = (_parse_long_date(page.facts.get("date"))
                   or _parse_long_date(page.facts.get("closing date"))
                   or _parse_long_date(page.facts.get("start date"))
                   or stub.hint_date or page.dc_date)

        summary = item.description if item else ""
        headline = f"{title}\n{summary}\n{page.body[:1500]}"
        counted = regimes_in(f"{title}\n{summary}\n{text}")
        regimes = [r for r, _ in counted]
        relations = [
            TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=r.name, dst_id=r.id,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            )
            for r in regimes
        ]

        tag = register.tag if register else (entry.tag if entry else self.collection)
        topic_tags = ["ico", "uk", tag, *types,
                      *( [_slug(sector)] if sector else [] ),
                      *(r.tag for r in regimes)]

        extra: dict = {
            "issuer": "ico",
            "collection": self.collection,
            "action_types": types or None,
            "sector": sector,
            "status": page.facts.get("status"),
            "consultation_opened": page.facts.get("start date"),
            "consultation_closed": page.facts.get("closing date"),
            "summary": (item.description if item else None),
            "regimes": [r.id for r in regimes] or None,
            "documents": attachments or None,
            "related": [dict(x) for x in page.related] or None,
            "page_id": page.page_id,
            "url": stub.landing_url,
            "contenthash": stub.hints.get("contenthash"),
            "jurisdiction": "gb",
            **({"needs_ocr": True} if needs_ocr else {}),
        }
        # One instrument dominates ⇒ say so, twice and for two different readers:
        # ``citation_default_instrument`` returns an orphaned "regulation 21" later in
        # the notice to the right law, and ``statutory_basis`` lets the extractor bind
        # this document's own "the Act" / "the Regulations" (citations.stage).
        host = dominant_regime(counted, regimes_in(headline))
        if host is not None:
            extra["citation_default_instrument"] = {"id": host.id, "kind": host.kind}
            extra["statutory_basis"] = host.name
            extra["regime"] = host.id

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=(register.doc_type if register else DocType.GUIDANCE),
            title=title,
            court="ICO",
            decision_date=decided,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=text.encode("utf-8"),
            raw_ext="txt",
            text=text,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[t for t in dict.fromkeys(topic_tags) if t],
            extra={k: v for k, v in extra.items() if v not in (None, [], "")},
        )

    # -- attachments -------------------------------------------------------------
    def _attachment(self, att: Attachment, *, budget: int) -> tuple[str, bool, int]:
        """(text, ocr_unavailable, bytes_downloaded) for one attachment, cached per run.

        A missing or unreadable attachment must never lose the item: the HTML page is
        still the record. Only a rate limit propagates.
        """
        if att.url in self._attachment_text:
            return self._attachment_text[att.url], False, 0
        if budget <= 0 or (att.size or 0) > budget:
            return "", False, 0
        from ..extraction import extract_bytes

        try:
            raw = self._get(att.url).content
        except FetchError:
            self._attachment_text[att.url] = ""
            return "", False, 0
        ext = att.ext or "pdf"
        text = (extract_bytes(raw, ext=ext,
                              mime="application/pdf" if ext == "pdf" else None).text or "")
        ocr_unavailable = False
        if ext == "pdf" and not text.strip():
            from .edpb import ocr_pdf

            ocr = ocr_pdf(raw)
            if ocr:
                text = ocr
            else:
                ocr_unavailable = True
        text = text.strip()
        self._attachment_text[att.url] = text
        return text, ocr_unavailable, len(raw)
