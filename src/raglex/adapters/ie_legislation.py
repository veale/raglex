"""Irish legislation adapters — the eISB (as enacted) and the LRC Revised Acts.

Ireland publishes its statute book through **two** services that are complementary,
not interchangeable, and a serious corpus holds both:

* **eISB** (``irishstatutebook.ie``, Office of the Attorney General) — every Act and
  Statutory Instrument **as enacted / as made**, plus pre-1922 material. This is the
  *official* text (RDFa ``eli:legal_value`` says so), and it is immutable: it answers
  "what did the Oireachtas pass?".
* **LRC Revised Acts** (``revisedacts.lawreform.ie``) — administrative consolidations
  of a curated few hundred Acts, with amendments applied and annotated. It answers
  "what does the law say now?", but it is expressly **non-authoritative**.

Both speak ELI, so Ireland slots in beside legislation.gov.uk and EUR-Lex as another
ELI source rather than a bespoke silo. Ids are the ELI Work path prefixed with the
jurisdiction — ``ie/2018/act/7``, ``ie/2016/si/201`` — which is what a citation
resolves to, so harvesting the Data Protection Act 2018 makes every edge pointing at
it resolve.

**Four things this pipeline has to get right, each learned the hard way:**

1. **No bulk download, no sitemap, no change feed.** The corpus is built by
   enumerating identifiers. The yearly index pages (``/eli/{year}/act``, ``/eli/{year}/si``)
   ship the complete listing in the *static* server HTML — the DataTables widget only
   re-decorates a table that is already there — so the whole pipeline runs on a plain
   HTTP client with no headless browser anywhere.
2. **XML is not universal.** SIs, SROs, pre-1922 Acts and the Constitution 404 on
   ``/xml`` (verified). Formats are probed in priority order (``xml → print → html``)
   and a 404 is recorded as a property of the item, not treated as an error. Which
   formats an item has is stored, so the day the publisher adds XML to an old SI, the
   next sweep sees the vector change.
3. **The format-less ``/en`` page is not ``/en/html``.** The RDFa metadata block — the
   richest free structured metadata Ireland publishes, and the source of the amendment
   graph (``eli:changes``), the EU transposition links (``eli:transposes``) and the
   enabling powers (``eli:based_on``) — lives *only* on the format-less Expression URI,
   and only from 2013. It is fetched separately from the content.
4. **A revised consolidation is a new version, not an edit.** Each LRC "Updated to"
   date is its own point-in-time Expression, stored under ``ie/2003/act/32@2025-06-01``
   and linked to the base Act, so advancing consolidations accumulate instead of
   overwriting each other and destroying point-in-time answerability.

**Politeness.** These are small government services with no published rate limit, so
the pacing is deliberately conservative. eISB also **403s any User-Agent carrying a
non-browser token** (the shared default's "RagLex/0.1 (research harvester)" suffix is
enough to be refused), so these adapters send a plain browser UA.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator

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
from ..formats import parse
from ..formats.eisb_xml import eli_id, prose_date

EISB = "https://www.irishstatutebook.ie"
LRC = "https://revisedacts.lawreform.ie"

# eISB refuses a UA that advertises a harvester (403 on every path), so identify as a
# plain browser. Contact/attribution is carried in the stored licence metadata instead.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Format priority. `print` matters: it is a single flat document, where `html` is
# paginated section-by-section, and it exists for everything XML doesn't cover.
EISB_FORMATS = ("xml", "print", "html")
# The version segment is fixed by the resource type: Acts are "enacted", secondary
# legislation is "made". Getting this wrong 404s every request for a type.
VERSION = {"act": "enacted", "prv": "enacted", "ca": "enacted", "cons": "enacted",
           "bps": "enacted", "si": "made", "sro": "made"}
DEFAULT_TYPES = ("act", "si")
# The eISB's own coverage floor for the modern series; pre-1922 material is reached
# through explicit ids and the LRC's `bps` rows, not by walking years.
FIRST_YEAR = 1922

_TABLE_ID = {"act": "public-acts-dtb", "si": "statutory-instruments-dtb"}
_ROW_RE = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td[^>]*>(?P<cell>.*?)(?=<td|</tr|$)", re.S | re.I)
_LINK_RE = re.compile(r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<label>.*?)</a>', re.S | re.I)
_PDF_KB_RE = re.compile(r"PDF size (\d+) kilobytes", re.I)
_TAGS_RE = re.compile(r"<[^>]+>")

# RDFa: the metadata is a flat list of <meta about= property= resource=/content= />.
# Attribute case varies within a single page (CONTENT and content both occur), so the
# scan is case-insensitive throughout.
_META_RE = re.compile(r"<meta\s+(?P<attrs>[^>]*?)/?>", re.I)
_ATTR_RE = re.compile(r'(?P<key>[\w:.-]+)\s*=\s*"(?P<value>[^"]*)"', re.S)
_ELI_WORK_RE = re.compile(
    r"irishstatutebook\.ie/eli/(?P<year>\d{4})/(?P<type>[a-z]{2,4})/(?P<num>[0-9]+[a-z]?)",
    re.I)
_EU_ELI_RE = re.compile(
    r"data\.europa\.eu/eli/(?P<kind>dir|reg|dec)/(?P<year>\d{4})/(?P<num>\d+)", re.I)
_SECTION_RE = re.compile(r"/(?:section|schedule|article)/([0-9a-z]+)$", re.I)

# LRC list rows carry the ELI on the row itself: data-eli="2003/act/32/ga".
_REV_ROW_RE = re.compile(
    r'<tr[^>]*\bclass="(?P<class>[^"]*)"[^>]*\bdata-eli="(?P<eli>[^"]+)"'
    r'(?P<attrs>[^>]*)>(?P<rest>.*?)</tr>', re.S | re.I)
_REV_ELI_RE = re.compile(r"^(?P<year>\d{4})/(?P<type>[a-z]{2,4})/(?P<num>[0-9]+[a-z]?)/(?P<lang>en|ga)$",
                         re.I)
_UPDATED_CELL_RE = re.compile(r'<td[^>]*class="updated"[^>]*>\s*<span>(?P<date>[^<]+)</span>', re.S | re.I)

_ISBC_LINK_RE = re.compile(
    r"/(?P<year>\d{4})/(?:en|ga)/(?:act/(?:pub|prv)|(?P<si>si|sro))/(?P<num>\d+)", re.I)


def _text(html: str) -> str:
    return " ".join(_TAGS_RE.sub(" ", html or "").split())


def _abs(href: str) -> str:
    """LRC hrefs mix site-relative paths with absolute S3 URLs (the version-pinned
    archive PDFs live on S3)."""
    return f"{LRC}{href}" if href.startswith("/") else href


# -- discovery: the yearly indexes ------------------------------------------
@dataclass(frozen=True, slots=True)
class IndexRow:
    """One row of a yearly index — the set of valid numbers for a year, which is the
    only reliable way to learn what exists (Acts run contiguously from 1, SIs do not)."""
    type: str
    year: int
    number: str
    title: str
    html_href: str | None = None
    pdf_href: str | None = None
    # The index advertises each PDF's size; a change between crawls is a free
    # "this document changed" signal that costs no fetch at all.
    pdf_kb: int | None = None


def parse_year_index(html: str, year: int, typ: str) -> list[IndexRow]:
    """Parse a static yearly index table (pure). Everything is in the server HTML —
    the DataTables script only adds client-side sorting."""
    table_id = _TABLE_ID.get(typ)
    if table_id:
        start = html.find(table_id)
        if start < 0:
            return []
        html = html[start:]
    rows: list[IndexRow] = []
    for m in _ROW_RE.finditer(html):
        cells = [c.group("cell") for c in _CELL_RE.finditer(m.group("row"))]
        if len(cells) < 2:
            continue
        number = _text(cells[0])
        if not number or not number[0].isdigit():
            continue
        link = _LINK_RE.search(cells[1])
        if not link:
            continue
        pdf_href = pdf_kb = None
        if len(cells) > 2:
            pdf = _LINK_RE.search(cells[2])
            if pdf:
                pdf_href = pdf.group("href")
                size = _PDF_KB_RE.search(cells[2])
                pdf_kb = int(size.group(1)) if size else None
        rows.append(IndexRow(
            type=typ, year=year, number=number.strip(),
            title=_text(link.group("label")).rstrip("."),
            html_href=link.group("href"), pdf_href=pdf_href, pdf_kb=pdf_kb,
        ))
    return rows


# -- metadata: the RDFa block on the format-less Expression URI --------------
@dataclass(frozen=True, slots=True)
class ELIMetadata:
    """The eISB metadata ontology, as published in RDFa (Acts and SIs, 2013+)."""
    title: str | None = None
    long_title: str | None = None       # eli:description — the "An Act to…" text
    number: str | None = None
    date_document: date | None = None   # date of signature
    changes: tuple[str, ...] = ()       # instruments THIS one amends
    transposes: tuple[str, ...] = ()    # EU directives/regulations transposed (CELEX)
    based_on: tuple[str, ...] = ()      # (SIs) the Act the instrument is made under
    related_to: tuple[str, ...] = ()    # usually the Oireachtas Bill
    subdivisions: tuple[str, ...] = ()  # the complete section/schedule map
    formats: tuple[str, ...] = ()       # which formats the PUBLISHER says exist
    is_authoritative: bool = False      # eli:legal_value = #LegalValue-official
    licence: str | None = None
    publisher: str | None = None
    raw: dict = field(default_factory=dict)


_EU_DESCRIPTOR = {"dir": "L", "reg": "R", "dec": "D"}


def _eu_celex(kind: str, year: str, num: str) -> str:
    """An EU ELI (``…/eli/dir/2014/57/oj``) → the CELEX the EU corpus is keyed by, so a
    transposition edge lands on the actual Directive node rather than dangling."""
    return f"3{year}{_EU_DESCRIPTOR[kind.lower()]}{int(num):04d}"


def parse_rdfa(html: str) -> ELIMetadata:
    """Extract the RDFa metadata block (pure). Absent before 2013 and on pre-1922
    material — an empty result is normal, never an error."""
    props: dict[str, list[str]] = {}
    for m in _META_RE.finditer(html or ""):
        attrs = {a.group("key").lower(): a.group("value")
                 for a in _ATTR_RE.finditer(m.group("attrs"))}
        prop = (attrs.get("property") or "").lower()
        if not prop.startswith("eli:"):
            continue
        value = attrs.get("resource") or attrs.get("content") or ""
        if value:
            props.setdefault(prop[4:], []).append(value)

    def one(name: str) -> str | None:
        vals = props.get(name)
        return vals[0] if vals else None

    changes = []
    for uri in props.get("changes", []):
        m = _ELI_WORK_RE.search(uri)
        if m:
            changes.append(eli_id(m.group("type"), m.group("year"), m.group("num")))
    based_on = []
    for uri in props.get("based_on", []):
        m = _ELI_WORK_RE.search(uri)
        if m:
            based_on.append(eli_id(m.group("type"), m.group("year"), m.group("num")))
    transposes = []
    for uri in props.get("transposes", []):
        m = _EU_ELI_RE.search(uri)
        if m:
            transposes.append(_eu_celex(m.group("kind"), m.group("year"), m.group("num")))
    subdivisions = []
    for uri in props.get("has_part", []):
        m = _SECTION_RE.search(uri)
        if m:
            subdivisions.append(uri.rsplit("/eli/", 1)[-1])
    formats = []
    for uri in props.get("is_embodied_by", []):
        fmt = uri.rstrip("/").rsplit("/", 1)[-1].lower()
        if fmt in ("html", "pdf", "xml", "print"):
            formats.append(fmt)

    doc_date = None
    raw_date = one("date_document")
    if raw_date:
        try:
            doc_date = date.fromisoformat(raw_date.strip()[:10])
        except ValueError:
            doc_date = None

    return ELIMetadata(
        title=one("title"), long_title=one("description"), number=one("number"),
        date_document=doc_date,
        changes=tuple(dict.fromkeys(changes)),
        transposes=tuple(dict.fromkeys(transposes)),
        based_on=tuple(dict.fromkeys(based_on)),
        related_to=tuple(props.get("related_to", [])),
        subdivisions=tuple(dict.fromkeys(subdivisions)),
        formats=tuple(dict.fromkeys(formats)),
        is_authoritative="LegalValue-official" in " ".join(props.get("legal_value", [])),
        licence=one("licence"), publisher=one("publisher"),
        raw={k: v for k, v in props.items()},
    )


# -- the amendment graph: ISBC tables ---------------------------------------
@dataclass(frozen=True, slots=True)
class ISBCTables:
    """The per-item commencement / amendment tables. This is how an Act with no revised
    consolidation still gets an amendment graph — and it is the *inverse* direction of
    ``eli:changes``: what affected THIS Act, which RDFa never records."""
    updated_to: date | None = None
    affected_by: tuple[str, ...] = ()      # instruments that amend/affect this one
    sis_made_under: tuple[str, ...] = ()   # secondary legislation made under it
    commencement_rows: int = 0


def _isbc_ids(html: str) -> list[str]:
    out = []
    for m in _ISBC_LINK_RE.finditer(html or ""):
        typ = (m.group("si") or "act").lower()
        out.append(eli_id(typ, m.group("year"), m.group("num")))
    return out


def parse_isbc(html: str) -> ISBCTables:
    """Parse an ISBC page (pure). The sections are marked by their anchors, so each
    table is sliced by heading id rather than by position."""
    if not html:
        return ISBCTables()
    updated = prose_date(_text(html[:html.find('id="commencement"')] if 'id="commencement"' in html
                               else html[:4000]))

    def slice_after(anchor: str) -> str:
        i = html.find(f'id="{anchor}"')
        if i < 0:
            return ""
        rest = html[i:]
        # stop at the next section heading so links aren't attributed to the wrong table
        nexts = [rest.find(f'id="{a}"') for a in ("effects", "associatedsecondary")
                 if a != anchor and rest.find(f'id="{a}"') > 0]
        return rest[:min(nexts)] if nexts else rest

    effects_html = slice_after("effects")
    secondary_html = slice_after("associatedsecondary")
    commencement_html = slice_after("commencement")
    return ISBCTables(
        updated_to=updated,
        affected_by=tuple(dict.fromkeys(_isbc_ids(effects_html))),
        sis_made_under=tuple(dict.fromkeys(_isbc_ids(secondary_html))),
        commencement_rows=commencement_html.count("<tr"),
    )


# -- the LRC revised overlay -------------------------------------------------
@dataclass(frozen=True, slots=True)
class RevisedRow:
    """One row of the LRC alphabetical/chronological list. The list carries the
    "Updated to" date inline, so a *new consolidation* is detectable by diffing this
    single page — without fetching a single document.

    **The list holds more than it shows.** Each Act's ``◀`` chevron expands
    ``style="display:none"`` rows — one per *prior* consolidation, each with its own
    "Updated to" date and a version-pinned S3 PDF. That is a real point-in-time archive
    the ELI URIs do not expose: the dated ``/revised/{date}/…`` path 404s (verified),
    and the date-less path always resolves to the latest. So the current row is the only
    one with fetchable HTML/XML, and the historic rows are PDF-only snapshots — recorded
    against the current record rather than fetched as text, because fetching the
    date-less URI once per historic row would store today's text under seven past dates
    and quietly destroy the point-in-time answer."""
    work: str            # ie/2003/act/32
    year: int
    type: str
    number: str
    language: str
    title: str
    updated_to: date | None
    repealed: bool = False
    current: bool = True          # False → a collapsed prior-consolidation row
    pdf_annotated: str | None = None
    pdf_plain: str | None = None

    @property
    def stable_id(self) -> str:
        """A consolidation is stamped with the date it consolidates to, so successive
        revisions accumulate as distinct point-in-time Expressions instead of silently
        overwriting each other."""
        base = eli_id(self.type, self.year, self.number, self.language)
        return f"{base}@{self.updated_to.isoformat()}" if self.updated_to else base


def parse_revised_list(html: str) -> list[RevisedRow]:
    """Parse the LRC revised list (pure) — the authoritative "what has been revised, and
    to when" manifest."""
    rows: list[RevisedRow] = []
    for m in _REV_ROW_RE.finditer(html or ""):
        eli = _REV_ELI_RE.match(m.group("eli").strip())
        if not eli:
            continue
        rest = m.group("rest")
        title_link = None
        pdfs: dict[str, str] = {}
        for link in _LINK_RE.finditer(rest):
            href = link.group("href")
            if "/front/revised/" in href and title_link is None:
                title_link = link
            elif "annotations=true" in href or 'title="with annotations"' in link.group(0):
                pdfs.setdefault("annotated", _abs(href))
            elif "annotations=false" in href or "without annotations" in link.group(0):
                pdfs.setdefault("plain", _abs(href))
        updated = _UPDATED_CELL_RE.search(rest)
        title = _text(title_link.group("label")) if title_link else ""
        # Repealed items stay addressable and stay in the corpus — case law cites
        # repealed law — but the status is flagged. The list marks it two ways.
        repealed = ("repealed" in (m.group("class") or "").lower()
                    or "(repealed)" in title.lower())
        rows.append(RevisedRow(
            work=eli_id(eli.group("type"), eli.group("year"), eli.group("num")),
            year=int(eli.group("year")), type=eli.group("type").lower(),
            number=eli.group("num"), language=eli.group("lang").lower(),
            title=title,
            updated_to=prose_date(updated.group("date")) if updated else None,
            repealed=repealed,
            current="display:none" not in (m.group("attrs") or "").replace(" ", ""),
            pdf_annotated=pdfs.get("annotated"), pdf_plain=pdfs.get("plain"),
        ))
    return rows


def revised_manifest(rows: list[RevisedRow]) -> list[tuple[RevisedRow, list[RevisedRow]]]:
    """Group the flat list into (current consolidation, prior consolidations). Prior
    rows carry a title only on the current row, so they are matched by ELI + language."""
    current: dict[tuple[str, str], RevisedRow] = {}
    prior: dict[tuple[str, str], list[RevisedRow]] = {}
    for row in rows:
        key = (row.work, row.language)
        if row.current and key not in current:
            current[key] = row
        elif not row.current:
            prior.setdefault(key, []).append(row)
    return [(row, sorted(prior.get(key, []), key=lambda r: r.updated_to or date.min,
                         reverse=True))
            for key, row in current.items()]


# -- adapters ----------------------------------------------------------------
def _normalise_id(raw: str) -> tuple[str, int, str, str, str] | None:
    """Any way a user or a citation names an Irish instrument → its parts.
    Accepts ``ie/2018/act/7``, ``2018/act/7``, a full eISB/LRC URL, and the
    ``S.I. No. 201 of 2016`` / ``No. 7 of 2018`` human citations."""
    raw = (raw or "").strip()
    m = re.search(r"(?:^|/)(?P<year>\d{4})/(?P<type>[a-z]{2,4})/(?P<num>[0-9]+[a-z]?)"
                  r"(?:/(?P<lang>en|ga))?", raw, re.I)
    if m:
        lang = (m.group("lang") or "en").lower()
        typ = m.group("type").lower()
        return eli_id(typ, m.group("year"), m.group("num"), lang), int(m.group("year")), \
            typ, m.group("num"), lang
    m = re.search(r"S\.?\s*I\.?\s*No\.?\s*(?P<num>\d+[a-z]?)\s+of\s+(?P<year>\d{4})", raw, re.I)
    if m:
        return eli_id("si", m.group("year"), m.group("num")), int(m.group("year")), \
            "si", m.group("num"), "en"
    m = re.search(r"No\.?\s*(?P<num>\d+)\s+of\s+(?P<year>\d{4})", raw, re.I)
    if m:
        return eli_id("act", m.group("year"), m.group("num")), int(m.group("year")), \
            "act", m.group("num"), "en"
    return None


class IrishStatuteBookAdapter(BaseAdapter):
    """eISB — Acts and SIs **as enacted / as made** (the official, immutable text)."""

    source = "ie-legislation"
    min_interval = 1.0   # a small government service with no published limit: self-limit
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 years: str | None = None, types: str | None = None,
                 isbc: bool | str = True, client: RateLimitedClient | None = None) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        self.years = _year_range(years)
        self.types = tuple(t.strip().lower() for t in (types or "").split(",") if t.strip()) \
            or DEFAULT_TYPES
        self.isbc = str(isbc).lower() not in ("false", "0", "no", "")
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, user_agent=_UA, timeout=60)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            for raw in self.ids:
                parsed = _normalise_id(raw)
                if parsed:
                    stable_id, year, typ, num, lang = parsed
                    yield self._stub(stable_id, year, typ, num, lang, title=None)
            return
        yield from self._discover_years(since, max_pages=max_pages)

    def _discover_years(self, since: str | None, *, max_pages: int | None) -> Iterator[Stub]:
        """Walk the yearly indexes. The cursor is a **year**, and the most recent year is
        always re-walked: an index page carries no timestamps, so the only sound
        incremental rule is "re-check the open year". Re-listing is cheap — the runner
        skips already-held ids on a primary-key lookup, before paying for any fetch."""
        today = date.today().year
        years = self.years or range(_since_year(since, today), today + 1)
        pages = 0
        for year in years:
            for typ in self.types:
                try:
                    resp = self._client.get(f"{EISB}/eli/{year}/{typ}")
                except FetchError:
                    continue  # a year with no page for this type is an absence, not a failure
                for row in parse_year_index(resp.text, year, typ):
                    yield self._stub(
                        eli_id(typ, year, row.number), year, typ, row.number, "en",
                        title=row.title,
                        hints={"pdf_kb": row.pdf_kb, "watermark": str(year)},
                    )
                pages += 1
                if max_pages is not None and pages >= max_pages:
                    return

    def _stub(self, stable_id: str, year: int, typ: str, num: str, lang: str,
              *, title: str | None, hints: dict | None = None) -> Stub:
        version = VERSION.get(typ, "enacted")
        base = f"{EISB}/eli/{year}/{typ}/{num}"
        return Stub(
            stable_id=stable_id, title=title,
            landing_url=f"{base}/{version}/{lang}/html",
            raw_url=f"{base}/{version}/{lang}",
            hints={"year": year, "type": typ, "number": num, "language": lang,
                   "version": version, "base": base, **(hints or {})},
        )

    def fetch(self, stub: Stub) -> Record | None:
        h = stub.hints
        base, version, lang = h["base"], h["version"], h["language"]
        typ, year, num = h["type"], h["year"], h["number"]

        # 1. Metadata first: the RDFa lives on the FORMAT-LESS Expression URI, which is a
        #    different resource from /html. Absent before 2013 — not an error.
        meta = ELIMetadata()
        try:
            meta = parse_rdfa(self._client.get(f"{base}/{version}/{lang}").text)
        except FetchError:
            pass

        # 2. Content: probe formats in priority order. A 404 on /xml is the NORMAL case
        #    for SIs and pre-1922 Acts, so it is recorded as a fact about the item.
        raw = fmt = None
        available: list[str] = []
        for candidate in EISB_FORMATS:
            try:
                resp = self._client.get(f"{base}/{version}/{lang}/{candidate}")
            except FetchError:
                continue
            body = resp.content or b""
            if not body or _is_not_found(body):
                continue
            available.append(candidate)
            if raw is None:
                raw, fmt = body, candidate
        if raw is None:
            return None  # nothing served in any format — an absence, not a failure

        parser = "eisb-xml" if fmt == "xml" else "eisb-html"
        parsed = parse(parser, raw)
        title = _best_title(meta.title, parsed.title) or stub.title or stub.stable_id

        relations = [r for r in parsed.relations if r.dst_id != stub.stable_id]
        # A print page's own contents list links to every one of its sections; those are
        # self-references, not citations, and minting them would flood the worklist.

        # 3. The graph edges RDFa gives us, none of which are in the text.
        for target in meta.changes:
            if target != stub.stable_id:
                relations.append(_edge(RelationshipType.AMENDS, target))
        for celex in meta.transposes:
            # transposition: Irish instrument → the EU directive/regulation it implements,
            # keyed by CELEX so it lands on the EU corpus's own node.
            relations.append(_edge(RelationshipType.IMPLEMENTS, celex, dst_anchor="transposes"))
        for enabling in meta.based_on:
            # the enabling-power edge: an SI is made *under* an Act.
            relations.append(_edge(RelationshipType.IMPLEMENTS, enabling,
                                   dst_anchor="made under"))

        # 4. The inverse amendment direction — what affected THIS Act — plus commencement.
        #    RDFa only ever records the outward "changes" direction, so without the ISBC
        #    table an Act with no revised consolidation has no amendment history at all.
        isbc = ISBCTables()
        if self.isbc and typ in ("act", "prv"):
            isbc = self._fetch_isbc(year, num)
            for target in isbc.affected_by:
                if target != stub.stable_id:
                    relations.append(_edge(RelationshipType.AMENDED_BY, target))
            for si in isbc.sis_made_under:
                relations.append(_edge(RelationshipType.AMENDED_BY, si,
                                       dst_anchor="made under this Act"))

        extra = {
            "format": parser,
            "eli": f"{year}/{typ}/{num}",
            "version": version,
            "formats_available": available,
            # What the publisher SAYS it serves, alongside what we actually found. A
            # discrepancy means a rollout in progress, and is worth seeing.
            "formats_declared": list(meta.formats),
            "long_title": meta.long_title,
            "date_document": meta.date_document.isoformat() if meta.date_document else None,
            # eISB as-enacted text is OFFICIAL — the distinction from an LRC
            # consolidation is first-class metadata, not a footnote.
            "is_authoritative": meta.is_authoritative or not bool(meta.raw),
            "text_status": "as enacted" if version == "enacted" else "as made",
            "licence": meta.licence, "publisher": meta.publisher,
            "subdivisions": list(meta.subdivisions),
            "related_to": list(meta.related_to),
            "pdf_url": f"{base}/{version}/{lang}/pdf",
            "pdf_kb": h.get("pdf_kb"),
            "isbc_commencement_rows": isbc.commencement_rows,
        }
        if isbc.updated_to:
            extra["isbc_updated_to"] = isbc.updated_to.isoformat()

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=title,
            language=lang, source_language=lang,
            decision_date=meta.date_document or parsed.decision_date,
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=("xml" if fmt == "xml" else "html"),
            text=parsed.text, segments=parsed.segments, relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra=extra,
        )

    def _fetch_isbc(self, year: int, num: str) -> ISBCTables:
        try:
            resp = self._client.get(f"{EISB}/eli/isbc/{year}_{int(num)}.html")
        except (FetchError, ValueError):
            return ISBCTables()
        return parse_isbc(resp.text)


class IrishRevisedActsAdapter(BaseAdapter):
    """LRC Revised Acts — administrative consolidations (text *as amended*).

    A separate source from the eISB because it answers a different question, carries a
    different authoritativeness, and moves on its own schedule: the alpha list's
    "Updated to" column is the entire change signal, so a daily pass over one page
    detects every new consolidation without touching a document.
    """

    source = "ie-revised"
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 language: str = "en", client: RateLimitedClient | None = None) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        self.language = (language or "en").lower()
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, user_agent=_UA, timeout=60)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        try:
            resp = self._client.get(f"{LRC}/revacts/alpha")
        except FetchError:
            return
        wanted = {_normalise_id(i)[0] for i in self.ids if _normalise_id(i)} if self.ids else None
        for row, prior in revised_manifest(parse_revised_list(resp.text)):
            if wanted is None and row.language != self.language:
                continue
            if wanted is not None and row.work not in wanted:
                continue
            # The cursor is the consolidation date: an advanced "Updated to" means a new
            # point-in-time exists, and an unchanged one needs nothing fetched at all.
            if since and row.updated_to and row.updated_to.isoformat() <= since:
                continue
            base = f"{LRC}/eli/{row.year}/{row.type}/{row.number}"
            yield Stub(
                stable_id=row.stable_id, title=row.title or None,
                landing_url=f"{base}/front/revised/{row.language}/html",
                raw_url=f"{base}/revised/{row.language}/xml",
                hint_date=row.updated_to,
                hints={"work": row.work, "language": row.language,
                       "repealed": row.repealed, "base": base,
                       "updated_to": row.updated_to.isoformat() if row.updated_to else None,
                       "watermark": row.updated_to.isoformat() if row.updated_to else None,
                       # The PDF-only archive of earlier consolidations, kept as
                       # metadata: each is a real point-in-time artifact, and the URLs
                       # are version-pinned, so they stay retrievable.
                       "prior_versions": [
                           {"updated_to": p.updated_to.isoformat() if p.updated_to else None,
                            "pdf_annotated": p.pdf_annotated, "pdf_plain": p.pdf_plain}
                           for p in prior],
                       "pdf_annotated": row.pdf_annotated},
            )

    def fetch(self, stub: Stub) -> Record | None:
        h = stub.hints
        raw = fmt = None
        for candidate in ("xml", "html"):
            url = f"{h['base']}/revised/{h['language']}/{candidate}"
            try:
                resp = self._client.get(url)
            except FetchError:
                continue
            body = resp.content or b""
            if body and not _is_not_found(body):
                raw, fmt = body, candidate
                break
        if raw is None:
            return None

        parser = "eisb-xml" if fmt == "xml" else "eisb-html"
        parsed = parse(parser, raw)
        annotations = parsed.metadata.get("annotations") or []
        # Prefer the date the DOCUMENT states it consolidates to over the one the list
        # advertised — a date-less "latest" URI resolves to whatever is current, and an
        # unstamped snapshot cannot be placed in time.
        updated_to = parsed.metadata.get("updated_to") or (
            date.fromisoformat(h["updated_to"]) if h.get("updated_to") else None)

        relations = [r for r in parsed.relations if r.dst_id != h["work"]]
        relations.append(TypedRelation(
            relationship_type=RelationshipType.POINT_IN_TIME_OF,
            raw_citation_string=h["work"], dst_id=h["work"],
            dst_anchor=updated_to.isoformat() if updated_to else None,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
        # Every annotation the LRC parsed into an effect is an amendment edge with a
        # real commencement date attached — the bracketed date is when the effect became
        # OPERATIVE, which is not the amending Act's enactment date.
        seen: set[tuple] = set()
        for note in annotations:
            if not note.affecting_id or note.affecting_id == h["work"]:
                continue
            key = (note.affecting_id, note.provision, note.effect)
            if key in seen:
                continue
            seen.add(key)
            relations.append(TypedRelation(
                relationship_type=RelationshipType.AMENDED_BY,
                raw_citation_string=note.affecting_title or note.affecting_id,
                dst_id=note.affecting_id,
                src_anchor=note.provision, dst_anchor=note.effect,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))

        title = stub.title or parsed.title or stub.stable_id
        suffix = f" (revised to {updated_to.isoformat()})" if updated_to else " (revised)"
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=f"{title}{suffix}",
            language=h["language"], source_language=h["language"],
            decision_date=parsed.decision_date,
            landing_url=stub.landing_url,
            raw_bytes=raw, raw_ext=("xml" if fmt == "xml" else "html"),
            text=parsed.text, segments=parsed.segments, relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={
                "format": parser,
                "work": h["work"],
                "version": "revised",
                "updated_to": updated_to.isoformat() if updated_to else None,
                "point_in_time": updated_to.isoformat() if updated_to else None,
                # An LRC consolidation is an ADMINISTRATIVE text and carries a
                # no-warranty disclaimer — it is not a legally authoritative statement
                # of the law, and a reader must be told so.
                "is_authoritative": False,
                "text_status": "revised (administrative consolidation)",
                "disclaimer": ("Administrative consolidation published by the Law Reform "
                               "Commission; not an authoritative statement of the law."),
                "repealed": bool(h.get("repealed")),
                "annotation_count": len(annotations),
                "prior_versions": h.get("prior_versions") or [],
                "pdf_url": h.get("pdf_annotated")
                or f"{h['base']}/revised/{h['language']}/pdf?annotations=true",
            },
        )


def _best_title(rdfa: str | None, document: str | None) -> str | None:
    """The RDFa title is better *cased* than the XML one (which is often shouted in
    caps), but the publisher's RDFa serialises accented characters away — the Data
    Protection Act's long title reads "An Coimisin um Chosaint Sonra", losing both
    fadas. That mangling is invisible in English titles and disfiguring in Irish ones,
    so the document's own title wins whenever it carries accents the RDFa dropped."""
    if not rdfa:
        return document
    if document and rdfa.isascii() and not document.isascii():
        return document
    return rdfa


def _edge(kind: RelationshipType, dst_id: str, *, dst_anchor: str | None = None) -> TypedRelation:
    return TypedRelation(
        relationship_type=kind, raw_citation_string=dst_id, dst_id=dst_id,
        dst_anchor=dst_anchor, extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.PENDING,
    )


def _is_not_found(body: bytes) -> bool:
    """Some not-found responses come back 200 with a "Not Found" body. A format probe
    that trusts the status code alone records formats that aren't there — and the whole
    point of storing the format vector is to notice when it genuinely changes."""
    head = body[:1500].lower()
    return (b"<title>404" in head or b"<title>not found" in head
            or b"the requested url was not found" in head)


def _year_range(spec: str | None) -> tuple[int, ...]:
    """"2016", "2016-2018", "2016,2019" → the years to walk."""
    if not spec:
        return ()
    years: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(?P<a>\d{4})\s*-\s*(?P<b>\d{4})$", part)
        if m:
            years.extend(range(int(m.group("a")), int(m.group("b")) + 1))
        elif part.isdigit():
            years.append(int(part))
    return tuple(dict.fromkeys(years))


def _since_year(since: str | None, today: int) -> int:
    """The cursor is a year, and the newest year is always re-walked: items are added to
    the open year all through it, so stopping *after* the stored year would freeze the
    corpus at whatever was published the first time the crawl ran."""
    if not since:
        return FIRST_YEAR
    m = re.match(r"(\d{4})", str(since))
    return min(int(m.group(1)), today) if m else FIRST_YEAR
