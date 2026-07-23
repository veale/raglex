"""GDPRhub adapter — the noyb-run wiki of European data-protection decisions (§1.9/§4a).

GDPRhub (``gdprhub.eu``) is a MediaWiki whose every page is a structured **case
report**: a DPA decision or a court judgment turning on the GDPR, wrapped in a
fixed infobox template, followed by an English summary, noyb's own analysis, and —
crucially — a **machine translation of the original decision**. The site itself is
walled by an Anubis proof-of-work challenge, so we never crawl the HTML. Instead we
harvest the one surface it serves openly: the **Atom feed of new pages**

    index.php?title=Special:NewPages&feed=atom&hideredirs=1&limit=50&offset=<ts>

Every entry's ``<summary>`` carries the *entire rendered wikitext* of the page —
the infobox, the summary, the analysis and the translation all arrive in the feed,
so a harvested record needs **no second fetch**: ``discover`` parses each entry and
``fetch`` just assembles the ``Record`` from what discovery already stashed.

**Pagination & the 90-day horizon.** MediaWiki caps a Special-page feed at
``$wgFeedLimit`` (~50) whatever ``limit`` says, so the walk goes *backwards by
timestamp*: take the oldest entry seen, pass its creation time as ``offset``
(``YYYYMMDDHHMMSS``), repeat until a page comes back empty. But NewPages is built on
the ``recentchanges`` table, which MediaWiki prunes at ``$wgRCMaxAge`` — **90 days on
GDPRhub** (measured: the walk dead-ends ~90 days back, ~180 reports). So this feed is a
*rolling-window incremental* source — it reliably catches every new decision (~60-100 a
month) but it **cannot backfill the full ~4-5k historical corpus**; that would need a
route this adapter deliberately does not take (the site is Anubis-walled and the brief
is feed-only). Incremental runs stop at the first entry not newer than the watermark;
NewPages lists only *creations*, so later edits do not resurface.

**What each report becomes.** One document, stored as an administrative **decision**
(``DPAdecisionBOX``) or a **judgment** (``COURTdecisionBOX``) under its jurisdiction
(``court = dpa-xx`` — the same bucket key the EDPB OSS register uses, so a country's
DPA decisions from both sources sit together). Identity is the decision's own
identifier: the ECLI when the box carries one, else the native case number, both
minted as resolution aliases so a citation to either resolves here and so the report
ties to an authoritative copy of the same case if the corpus already holds one.

**Body text.** The machine translation is the document body (English, with the
original language recorded in ``source_language``). GDPRhub's own summary + analysis
ride in ``extra`` as a commentary block, surfaced in place of the body for any report
whose translation is absent — so a user or an MCP lookup always has something to read.

**Edges.** The infobox lists the GDPR articles applied, pinpointed — each becomes an
``interprets`` edge to the GDPR (32016R0679) with the article as the anchor. Other DP
instruments (the LED, the EUDPR, ePrivacy, 95/46, the Charter, the DSA/DMA/AI Act) are
*not* boxed by GDPRhub, so they are mined from the report text and the national-law
fields and emitted as ``interprets`` edges to their CELEX — provenance ``regex`` so
inferred references stay distinguishable from the structured GDPR ones. Original-source
links (the DPA's own decision page/PDF) are stored, never fetched: they render as
"See on <DPA>" and give an MCP bot the canonical pointer even though we hold no
original text for them.
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator
from urllib.parse import quote
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Stub,
    TypedRelation,
)

BASE = "https://gdprhub.eu/index.php"
API = "https://gdprhub.eu/api.php"
FEED_QS = "title=Special:NewPages&feed=atom&hideredirs=1&limit=50"
GDPR_CELEX = "32016R0679"
_ATOM = "{http://www.w3.org/2005/Atom}"
# MediaWiki caps titles-per-query at 50 for a normal client (500 for bots)
_TITLES_PER_QUERY = 50


def _api_json(html: str) -> dict | None:
    """The MediaWiki API returns JSON, but the stealth browser wraps it in
    ``<html><body>…`` and HTML-escapes it (``&`` → ``&amp;``, ``<`` → ``\\u003C`` stays
    escaped in the JSON). Strip the wrapper, unescape, and load the outermost object."""
    if not html:
        return None
    txt = _html.unescape(re.sub(r"<[^>]+>", "", html))
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(txt[i:j + 1])
    except json.JSONDecodeError:
        return None


def _page_url(title: str) -> str:
    return f"{BASE}?title={quote(title.replace(' ', '_'), safe='')}"


# ── DP instruments GDPRhub does NOT box → mined from the report text (§1A) ────
# Each: CELEX, a human label, and a pattern strict enough to avoid false hits (we
# prefer the instrument NUMBER or full name; bare initialisms that double as common
# words — "LED" as a screen — are deliberately not matched on their own).
@dataclass(frozen=True, slots=True)
class Regime:
    celex: str
    label: str
    pattern: re.Pattern


REGIMES: tuple[Regime, ...] = (
    Regime("32016L0680", "Directive (EU) 2016/680 (LED)",
           re.compile(r"\b2016/680\b|Law Enforcement Directive", re.I)),
    Regime("32018R1725", "Regulation (EU) 2018/1725 (EUDPR)",
           re.compile(r"\b2018/1725\b|\bEUDPR\b", re.I)),
    Regime("32002L0058", "Directive 2002/58/EC (ePrivacy)",
           re.compile(r"\b2002/58\b|e-?Privacy", re.I)),
    Regime("31995L0046", "Directive 95/46/EC",
           re.compile(r"\b95/46\b", re.I)),
    Regime("12012P", "Charter of Fundamental Rights",
           re.compile(r"Charter of Fundamental Rights|\bCFR\b|Article\s*[78]\s*(?:of the\s*)?(?:Charter|CFR)", re.I)),
    Regime("32022R2065", "Regulation (EU) 2022/2065 (DSA)",
           re.compile(r"\b2022/2065\b|Digital Services Act", re.I)),
    Regime("32022R1925", "Regulation (EU) 2022/1925 (DMA)",
           re.compile(r"\b2022/1925\b|Digital Markets Act", re.I)),
    Regime("32024R1689", "Regulation (EU) 2024/1689 (AI Act)",
           re.compile(r"\b2024/1689\b|Artificial Intelligence Act|\bAI Act\b", re.I)),
)

# Jurisdiction name → ISO-3166 alpha-2, lower-case (the court-bucket key). Covers the
# EEA + UK + the supranational "European Union"; an unlisted jurisdiction falls back to
# a slug of its name so nothing is dropped.
_ISO2: dict[str, str] = {
    "austria": "at", "belgium": "be", "bulgaria": "bg", "croatia": "hr",
    "cyprus": "cy", "czech republic": "cz", "czechia": "cz", "denmark": "dk",
    "estonia": "ee", "finland": "fi", "france": "fr", "germany": "de",
    "greece": "gr", "hungary": "hu", "iceland": "is", "ireland": "ie",
    "italy": "it", "latvia": "lv", "liechtenstein": "li", "lithuania": "lt",
    "luxembourg": "lu", "malta": "mt", "netherlands": "nl", "norway": "no",
    "poland": "pl", "portugal": "pt", "romania": "ro", "slovakia": "sk",
    "slovenia": "si", "spain": "es", "sweden": "se",
    "united kingdom": "gb", "uk": "gb", "european union": "eu",
    "european data protection supervisor": "eu", "edps": "eu",
}


# ── pure parsing ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class FeedEntry:
    page_title: str      # the MediaWiki page title (durable upstream identity)
    display_title: str   # human title ("NAIH (Hungary) - NAIH-11443-3/2026")
    url: str             # the page URL (the entry <id>/<link>)
    updated: str         # ISO 8601 creation timestamp (the pagination cursor)
    summary_html: str    # the entry <summary> — full rendered wikitext, HTML-escaped


def parse_feed(xml_bytes: bytes) -> list[FeedEntry]:
    """Every entry of one Atom page (pure). Robust to how the page arrives: the raw
    ``<feed>`` XML, or — as the stealth browser returns it — the same feed wrapped in
    ``<html><body>…``. Tries a namespaced XML walk (``iter`` finds entries at any depth),
    then falls back to a tag-level regex so a wrapper or a single malformed node never
    loses the page."""
    out = _parse_feed_xml(xml_bytes)
    if out:
        return out
    return _parse_feed_regex(
        xml_bytes.decode("utf-8", "replace") if isinstance(xml_bytes, (bytes, bytearray))
        else str(xml_bytes))


def _parse_feed_xml(xml_bytes: bytes) -> list[FeedEntry]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[FeedEntry] = []
    for e in root.iter(f"{_ATOM}entry"):   # iter → found even under an <html><body> wrap
        title = (e.findtext(f"{_ATOM}title") or "").strip()
        eid = (e.findtext(f"{_ATOM}id") or "").strip()
        link_el = e.find(f"{_ATOM}link")
        url = (link_el.get("href") if link_el is not None else None) or eid
        updated = (e.findtext(f"{_ATOM}updated") or "").strip()
        summary = e.findtext(f"{_ATOM}summary") or ""
        if not (url and summary):
            continue
        out.append(FeedEntry(
            page_title=_page_title_from_url(url) or title, display_title=title,
            url=url, updated=updated, summary_html=summary,
        ))
    return out


_ENTRY_RE = re.compile(r"<entry\b.*?</entry>", re.S)


def _parse_feed_regex(text: str) -> list[FeedEntry]:
    def _tag(block: str, tag: str) -> str:
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, re.S)
        return m.group(1).strip() if m else ""

    out: list[FeedEntry] = []
    for block in _ENTRY_RE.findall(text):
        eid = _tag(block, "id")
        title = _tag(block, "title")
        lm = re.search(r"<link\b[^>]*\bhref=\"([^\"]+)\"", block)
        url = (lm.group(1) if lm else "") or eid
        summary = _tag(block, "summary")
        if not (url and summary):
            continue
        out.append(FeedEntry(
            page_title=_page_title_from_url(url) or title, display_title=title,
            url=url, updated=_tag(block, "updated"), summary_html=summary,
        ))
    return out


def _page_title_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"[?&]title=([^&]+)", url)
    return _html.unescape(m.group(1)) if m else None


_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_BOX = re.compile(r"\{\{(DPAdecisionBOX|COURTdecisionBOX)(.*?)\n?\}\}", re.S)
_PARAM = re.compile(r"\|([A-Za-z0-9_ ]+?)\s*=\s*(.*)$")
# a top-level (== .. ==) section heading only — level-3 (=== Facts ===) sub-headings
# stay *inside* their section so "English Summary" keeps its Facts/Holding body
_HEADING = re.compile(r"^\s*(={2,6})\s*(.+?)\s*\1\s*$")
_PRE = re.compile(r"<pre>(.*?)</pre>", re.S | re.I)


def _wiki_lines(fragment: str) -> list[str]:
    """A wikitext fragment (the feed uses ``<br />`` as line breaks) → clean lines."""
    text = _html.unescape(_BR.sub("\n", fragment))
    return [ln.strip() for ln in text.split("\n")]


def _flatten(fragment: str) -> str:
    """Strip tags + unescape a fragment to plain text, collapsing whitespace."""
    text = _html.unescape(_TAG.sub(" ", _BR.sub("\n", fragment)))
    return re.sub(r"[ \t]+", " ", text).strip()


# GDPRhub seeds empty Comment/Further-Resources sections with an italic placeholder;
# a report whose only "analysis" is this has none, so it must not read as commentary.
_PLACEHOLDER = re.compile(r"^\s*share (your comments|blogs or news articles) here!?\s*$", re.I)


def _clean_prose(fragment: str) -> str:
    """Flatten a narrative section and tidy wikitext: drop ``''italic''``/``'''bold'''``
    quotes, turn ``=== Sub-heading ===`` into a bare label, and blank out the editor
    placeholders so an un-analysed report exposes no commentary."""
    lines_out: list[str] = []
    for raw in _BR.sub("\n", fragment).split("\n"):
        line = _flatten(raw)
        line = re.sub(r"={2,}\s*(.+?)\s*={2,}", r"\1", line)   # heading markers → label
        line = re.sub(r"'{2,5}", "", line)                      # wiki bold/italic
        line = line.strip()
        if line and not _PLACEHOLDER.match(line):
            lines_out.append(line)
    return "\n".join(lines_out).strip()


@dataclass(frozen=True, slots=True)
class ParsedReport:
    box_type: str                 # DPAdecisionBOX | COURTdecisionBOX | ""
    params: dict[str, str]        # infobox fields (empty values dropped)
    summary: str                  # == English Summary == (facts + holding), plain text
    analysis: str                 # == Comment == — GDPRhub's own note, plain text
    further: str                  # == Further Resources ==, plain text
    translation: str              # the machine-translated original decision, plain text
    plain: str                    # the whole report as plain text (for regime mining)


def _canonical(s: str) -> str:
    """Collapse the feed's escaping to one canonical level. MediaWiki double-escapes
    ``<pre>`` blocks, and the stealth browser fetch leaves the whole payload one level
    deeper than the raw XML (ElementTree auto-unescapes once, a regex extract does not).
    Unescaping to a fixed point makes ``<br />``, the ``== headings ==`` and the ``<pre>``
    translation blocks real regardless of which path delivered the page."""
    for _ in range(3):
        u = _html.unescape(s)
        if u == s:
            return s
        s = u
    return s


def parse_report(summary_html: str) -> ParsedReport:
    """One feed entry's wikitext (pure) → its infobox + narrative sections."""
    summary_html = _canonical(summary_html)
    box_type, params = "", {}
    m = _BOX.search(summary_html)
    if m:
        box_type = m.group(1)
        for line in _wiki_lines(m.group(2)):
            pm = _PARAM.match(line)
            if pm:
                key, val = pm.group(1).strip(), pm.group(2).strip()
                # a field may legitimately repeat (two-instance appeal boxes); first wins
                if val and key not in params:
                    params[key] = val

    sections = _split_sections(summary_html)
    translation = ""
    tr_raw = sections.get("english machine translation of the decision", "")
    if tr_raw:
        pre = _PRE.search(tr_raw)
        translation = _flatten(pre.group(1) if pre else tr_raw)
        # drop the boilerplate lead sentence if that is all a <pre>-less section held
        if translation.lower().startswith("the decision below is a machine translation"):
            translation = translation.split(".", 1)[-1].strip()

    return ParsedReport(
        box_type=box_type,
        params=params,
        summary=_clean_prose(sections.get("english summary", "")),
        analysis=_clean_prose(sections.get("comment", "")),
        further=_clean_prose(sections.get("further resources", "")),
        translation=translation,
        plain=_flatten(summary_html),
    )


def _split_sections(summary_html: str) -> dict[str, str]:
    """Slice the wikitext on its ``== Heading ==`` seams → {lower heading: body html}."""
    lines = _BR.sub("\n", summary_html).split("\n")
    out: dict[str, list[str]] = {}
    current = "_preamble"
    out[current] = []
    for ln in lines:
        h = _HEADING.match(_TAG.sub("", ln).strip())
        if h and len(h.group(1)) == 2:   # only == .. == starts a new section
            current = h.group(2).strip().lower()
            out.setdefault(current, [])
        else:
            out[current].append(ln)
    return {k: "\n".join(v) for k, v in out.items()}


def _iso2(jurisdiction: str) -> str:
    j = (jurisdiction or "").strip().lower()
    return _ISO2.get(j) or _slug(j) or "xx"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def stable_id_for(page_title: str) -> str:
    """Identity from the durable MediaWiki page title alone (not the jurisdiction), so
    ``discover`` can key a stub before parsing the infobox and dedup held reports."""
    return f"gdprhub/{_slug(page_title)}"


def _ddmmyyyy(value: str | None) -> date | None:
    m = re.match(r"\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", value or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _numbered(params: dict[str, str], prefix: str) -> list[str]:
    """Collapse ``Prefix_1..Prefix_N`` (1-based, gaps tolerated) into an ordered list."""
    out: list[tuple[int, str]] = []
    for k, v in params.items():
        m = re.fullmatch(rf"{re.escape(prefix)}_(\d+)", k)
        if m and v:
            out.append((int(m.group(1)), v))
    return [v for _, v in sorted(out)]


def build_relations(report: ParsedReport) -> list[TypedRelation]:
    """Infobox GDPR articles → structured ``interprets`` edges to the GDPR; other DP
    instruments mined from the report text → ``interprets`` edges to their CELEX."""
    rels: list[TypedRelation] = []
    seen: set[tuple[str, str | None]] = set()

    for art in _numbered(report.params, "GDPR_Article"):
        # e.g. "Article 13(1)(c) GDPR" → anchor "Article 13(1)(c)"
        anchor = re.sub(r"\s*GDPR\s*$", "", art).strip()
        key = (GDPR_CELEX, anchor.lower())
        if key in seen:
            continue
        seen.add(key)
        rels.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=art,
            dst_id=GDPR_CELEX, dst_anchor=anchor or None,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    if not rels and re.search(r"\bGDPR\b|2016/679", report.plain):
        rels.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string="GDPR",
            dst_id=GDPR_CELEX,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))

    for reg in REGIMES:
        if (reg.celex, None) in seen:
            continue
        if reg.pattern.search(report.plain):
            seen.add((reg.celex, None))
            rels.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=reg.label,
                dst_id=reg.celex,
                extracted_via=ExtractedVia.REGEX,
                resolution_status=ResolutionStatus.PENDING,
            ))
    return rels


def _original_sources(params: dict[str, str]) -> list[dict]:
    """The DPA/court's own decision links — stored, never fetched; rendered "See on …"."""
    out: list[dict] = []
    for i in range(1, 6):
        link = params.get(f"Original_Source_Link_{i}")
        if not link:
            continue
        out.append({k: v for k, v in {
            "name": params.get(f"Original_Source_Name_{i}"),
            "url": link,
            "language": params.get(f"Original_Source_Language_{i}"),
            "language_code": (params.get(f"Original_Source_Language__Code_{i}") or "").lower() or None,
        }.items() if v})
    return out


def _clean_case_number(value: str | None) -> str | None:
    """Drop a stray ``Case number:`` label some contributors type into the field, and
    treat the ``N/A`` placeholder some pages use as no case number."""
    if not value:
        return None
    cleaned = re.sub(r"^\s*case\s*number\s*:?\s*", "", value, flags=re.I).strip()
    if cleaned.lower() in ("n/a", "na", "n.a.", "-", "–", "none", "unknown"):
        return None
    return cleaned or None


def build_record(entry: FeedEntry, source: str) -> Record | None:
    """A parsed feed entry → the normalised administrative-decision / judgment record,
    or ``None`` when the page carries no case-report infobox (NewPages also lists the
    occasional template/portal page that is not a decision)."""
    report = parse_report(entry.summary_html)
    if not report.box_type:
        return None
    p = report.params
    is_court = report.box_type == "COURTdecisionBOX"

    jurisdiction = p.get("Jurisdiction", "")
    iso2 = _iso2(jurisdiction)
    court = f"court-{iso2}" if is_court else f"dpa-{iso2}"
    stable_id = stable_id_for(entry.page_title)

    ecli_raw = (p.get("ECLI") or "").strip()
    ecli = ecli_raw if ecli_raw.upper().startswith("ECLI:") else None
    case_number = _clean_case_number(p.get("Case_Number_Name"))

    source_lang = (p.get("Original_Source_Language__Code_1") or "").lower() or None
    # the body is the machine translation; if absent, the English summary stands in so
    # the document is never empty — the analysis is always available in `extra` too.
    body = report.translation or report.summary or None

    # native identifier(s) the corpus may cite this decision by → resolution aliases
    aliases: list[str] = []
    if case_number and (any(ch.isdigit() for ch in case_number) and len(case_number) > 4):
        aliases.append(case_number.casefold())

    national_law = [
        {"name": n, "url": u} for n, u in zip(
            _numbered(p, "National_Law_Name"), _numbered(p, "National_Law_Link") + [""] * 10)
    ]

    extra = {
        "gdprhub_url": entry.url,
        "page_title": entry.page_title,
        "case_number": case_number,
        "jurisdiction": jurisdiction or None,
        "dpa": p.get("DPA_With_Country") or p.get("DPA_Abbrevation"),
        "court_name": p.get("Court_With_Country") or p.get("Court_English_Name"),
        "type": p.get("Type"),
        "outcome": p.get("Outcome"),
        "fine": p.get("Fine"),
        "currency": p.get("Currency"),
        "date_decided": p.get("Date_Decided"),
        "date_published": p.get("Date_Published"),
        "date_started": p.get("Date_Started"),
        "parties": [x for x in _numbered(p, "Party_Name")] or None,
        "appeal_from": {k: p.get(f"Appeal_From_{k}") for k in
                        ("Body", "Case_Number_Name", "Status", "Link") if p.get(f"Appeal_From_{k}")} or None,
        "appeal_to": {k: p.get(f"Appeal_To_{k}") for k in
                      ("Body", "Case_Number_Name", "Status", "Link") if p.get(f"Appeal_To_{k}")} or None,
        "original_sources": _original_sources(p) or None,
        "national_law": national_law or None,
        # GDPRhub's own case report — the commentary surfaced in place of the body when
        # no original translation exists, and alongside it otherwise (§1.9 secondary).
        "gdprhub_summary": report.summary or None,
        "gdprhub_analysis": report.analysis or None,
        "further_resources": report.further or None,
        "has_translation": bool(report.translation),
        "contributor": p.get("Initial_Contributor"),
        "aliases": aliases or None,
        "secondary_source": True,   # a wiki case report, not the authoritative original
    }

    topic_tags = ["gdprhub", court]
    for tag in (p.get("Type"), p.get("Outcome")):
        if tag:
            topic_tags.append(_slug(tag))

    return Record(
        source=source,
        stable_id=stable_id,
        doc_type=DocType.JUDGMENT if is_court else DocType.DECISION,
        title=entry.display_title or entry.page_title,
        court=court,
        decision_date=_ddmmyyyy(p.get("Date_Decided")),
        language="en",
        source_language=source_lang,
        ecli=ecli,
        landing_url=entry.url,
        raw_bytes=entry.summary_html.encode("utf-8"),
        raw_ext="html",
        text=body,
        relations=build_relations(report),
        topic_tags=topic_tags,
        extracted_via=ExtractedVia.STRUCTURED,
        extra={k: v for k, v in extra.items() if v not in (None, [], "", {})},
    )


# ── adapter ──────────────────────────────────────────────────────────────────
class GDPRhubAdapter(BaseAdapter):
    """Harvest GDPRhub case reports. Two discovery modes, one parser:

    * **feed** (default) — the NewPages Atom feed: incremental, but only the last ~90
      days (MediaWiki prunes recentchanges). The recurring watch.
    * **api=true** — the MediaWiki API (``list=allpages`` + batched ``revisions``): the
      whole catalogue, for the one-time historical backfill the feed cannot reach.

    Both are Anubis-walled, so every request goes through the stealth tier; both yield the
    same ``{{DPAdecisionBOX}}`` wikitext, so build_record and the ``gdprhub/<slug>``
    identity are shared — an API-backfilled page and its later feed re-appearance are the
    same node."""

    source = "gdprhub"
    min_interval = 4.0            # polite: the feed pages are large and browser-fetched
    requires_js = True            # the Anubis PoW challenge needs a real browser
    requires_proxy = False

    # a hard ceiling on a runaway backfill walk (≈ pages × 50 reports)
    _MAX_PAGES_BACKFILL = 200

    def __init__(self, *, fetcher=None, max_pages: int | None = None,
                 api: bool | str = False) -> None:
        # the whole report content rides in the feed, so discovery caches each parsed
        # entry here and fetch() rebuilds from it — no per-document network call
        self._cache: dict[str, FeedEntry] = {}
        self._fetcher = fetcher
        self._max_pages_cfg = max_pages
        # ``api=true`` switches discovery from the 90-day NewPages feed to the MediaWiki
        # **API** (``list=allpages`` + batched ``revisions`` content) — the full-catalogue
        # historical backfill the feed cannot reach. See _discover_api.
        self.api = bool(api) and str(api).lower() not in ("0", "false", "no")

    def _feed(self):
        if self._fetcher is None:
            from ..scraping.fetcher import get_fetcher
            # stealth → scrapling-mcp when RAGLEX_SCRAPLING_MCP_URL is set (asahi), else
            # in-process Camoufox; either clears the Anubis challenge the raw feed hides behind
            self._fetcher = get_fetcher("stealth", source=self.source,
                                        min_interval=self.min_interval, requires_js=True)
        return self._fetcher

    def _page(self, offset: str | None) -> list[FeedEntry]:
        url = f"{BASE}?{FEED_QS}" + (f"&offset={offset}" if offset else "")
        page = self._feed().fetch(url)
        return parse_feed((page.html or "").encode("utf-8"))

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.api:
            yield from self._discover_api(max_pages)
        else:
            yield from self._discover_feed(since, max_pages)

    def _discover_api(self, max_pages: int | None) -> Iterator[Stub]:
        """Full-catalogue backfill via the MediaWiki API (bypasses the feed's 90-day
        horizon). ``list=allpages`` enumerates every main-namespace page in 500-title
        batches (``apcontinue`` cursor); each batch's **wikitext** is then pulled 50 pages
        at a time via ``prop=revisions&rvslots=main`` — the same ``{{DPAdecisionBOX}}``
        source the feed carried, so build_record is unchanged. The content is cached on the
        stub, so fetch() stays a pure build. ``max_pages`` caps the number of allpages
        batches (each ≈ 500 pages); omit for the whole catalogue."""
        cont: str | None = None
        batches = 0
        seen: set[str] = set()
        while True:
            # apfilterredir=nonredirects drops the #REDIRECT alias pages (an alternate
            # case name → its canonical decision, which allpages lists separately), so we
            # enumerate only real pages and don't pay to fetch a redirect's stub wikitext.
            url = (f"{API}?action=query&list=allpages&aplimit=500&apfilterredir=nonredirects"
                   "&format=json" + (f"&apcontinue={quote(cont, safe='')}" if cont else ""))
            data = _api_json((self._feed().fetch(url).html or ""))
            if not data:
                return
            titles = [p["title"] for p in data.get("query", {}).get("allpages", [])
                      if p.get("title") and p["title"] not in seen]
            seen.update(titles)
            for i in range(0, len(titles), _TITLES_PER_QUERY):
                chunk = titles[i:i + _TITLES_PER_QUERY]
                for title, wikitext in self._wikitext_batch(chunk).items():
                    yield Stub(
                        stable_id=stable_id_for(title),
                        landing_url=_page_url(title),
                        raw_url=_page_url(title),
                        title=title,
                        hints={"api_title": title, "wikitext": wikitext},
                    )
            cont = (data.get("continue") or {}).get("apcontinue")
            batches += 1
            if not cont or (max_pages is not None and batches >= max_pages):
                return

    def _wikitext_batch(self, titles: list[str]) -> dict[str, str]:
        """Current wikitext for up to 50 titles in one request (``prop=revisions``)."""
        if not titles:
            return {}
        url = (f"{API}?action=query&titles={quote('|'.join(titles), safe='|')}"
               "&prop=revisions&rvprop=content&rvslots=main&format=json")
        data = _api_json((self._feed().fetch(url).html or ""))
        out: dict[str, str] = {}
        for page in (data or {}).get("query", {}).get("pages", {}).values():
            revs = page.get("revisions") or []
            if not revs:
                continue
            rev = revs[0]
            # rvslots=main nests the content under slots.main; tolerate the legacy shape
            wt = (rev.get("slots", {}).get("main", {}) or {}).get("*") or rev.get("*")
            if page.get("title") and wt:
                out[page["title"]] = wt
        return out

    def _discover_feed(self, since: str | None, max_pages: int | None) -> Iterator[Stub]:
        """Walk the NewPages feed newest-first. Incremental (``since`` set): stop at the
        first entry not newer than the watermark. Backfill (no ``since``): page backwards
        by ``offset`` until a page is empty or the page budget is spent."""
        cap = max_pages or self._max_pages_cfg or (None if since else self._MAX_PAGES_BACKFILL)
        offset: str | None = None
        seen_urls: set[str] = set()
        pages = 0
        while True:
            entries = self._page(offset)
            if not entries:
                return
            oldest_ts: str | None = None
            progressed = False
            for e in entries:
                if e.url in seen_urls:
                    continue
                seen_urls.add(e.url)
                progressed = True
                if oldest_ts is None or (e.updated and e.updated < oldest_ts):
                    oldest_ts = e.updated
                if since and e.updated and e.updated <= since:
                    return
                self._cache[e.url] = e
                yield Stub(
                    stable_id=stable_id_for(e.page_title),
                    landing_url=e.url,
                    raw_url=e.url,
                    title=e.display_title,
                    hint_date=_iso_date(e.updated),
                    hints={"url": e.url, "watermark": e.updated},
                )
            pages += 1
            if (cap is not None and pages >= cap) or not progressed:
                return
            offset = _offset_from(oldest_ts)
            if not offset:
                return

    def fetch(self, stub: Stub) -> Record | None:
        # API-backfill stubs carry their wikitext already (batched in discovery)
        wt = stub.hints.get("wikitext")
        if wt is not None:
            title = stub.hints.get("api_title") or stub.title or ""
            entry = FeedEntry(page_title=title, display_title=title.replace("_", " "),
                              url=stub.landing_url or _page_url(title),
                              updated="", summary_html=wt)
            return build_record(entry, self.source)
        url = stub.hints.get("url") or stub.landing_url
        entry = self._cache.get(url)
        if entry is None:
            # a fetch without the discovery cache (e.g. targeted re-fetch): re-pull the
            # single feed page whose offset brackets this entry is not possible, so read
            # the feed head and look for it — cheap and rare.
            for e in self._page(None):
                self._cache[e.url] = e
            entry = self._cache.get(url)
        if entry is None:
            return None
        return build_record(entry, self.source)


def _iso_date(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _offset_from(ts: str | None) -> str | None:
    """An ISO 8601 timestamp → the ``YYYYMMDDHHMMSS`` ``offset`` MediaWiki pages on."""
    d = None
    if ts:
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            d = None
    return d.strftime("%Y%m%d%H%M%S") if d else None
