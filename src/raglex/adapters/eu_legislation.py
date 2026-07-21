"""EU legislation adapter — CELLAR Formex (the published machine-readable format).

AKN4EU is the EU's interinstitutional drafting/exchange standard, but CELLAR does
not currently publish an ``akn`` manifestation for acts (verified: even the 2024
AI Act exposes only ``fmx4`` / ``xhtml`` / ``pdf``), so the reliable structured
route is **Formex 4** — its ``<ACT>`` content member carries the full ``<ARTICLE>``
hierarchy (99 articles for the GDPR). The stable_id is the CELEX, so harvesting
the GDPR resolves every "interprets 32016R0679" edge the CELLAR case adapter
emitted (§5b). When CELLAR starts publishing an AKN4EU manifestation, it's a new
``format`` parser — the adapter is unchanged.

**Discovery is a CELLAR SPARQL enumeration by default** — the full-catalogue path.
Naming CELEXes (``-o celex=32016R0679,32016L0680``) fetches exactly those; otherwise
``discover`` walks sector-3 legal acts (Regulations ``R``, Directives ``L``, Decisions
``D``) newest-first, paging with ``OFFSET``. An **incremental** run stops at the stored
document-date cursor; a **backfill** (no cursor, no page cap) walks the whole series.
``types=`` picks the descriptors, ``years=`` bounds the span.
"""

from __future__ import annotations

import re
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub, TypedRelation
from ..formats import parse

# A Directive CELEX (sector 3, descriptor L) — the only instruments that have national
# transposition measures, so the only ones we run the (extra) transposition query for.
_DIRECTIVE_RE = re.compile(r"^3\d{4}L\d", re.IGNORECASE)

CELEX_BASE = "https://publications.europa.eu/resource/celex"
SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
CDM = "http://publications.europa.eu/ontology/cdm#"
# Sector 3 = legal acts. R = Regulation, L = Directive, D = Decision — the legislative
# mass. The trailing anchor drops corrigenda (``32019L1153R(02)``), which are not
# separate instruments.
DEFAULT_TYPES = ("R", "L", "D")

# Consolidated EU primary-law documents. Citations use the CELEX stem (12016E),
# EUR-Lex displays /TXT, and ELI supplies the durable web identity. All forms and
# ordinary legal names must converge on the same held node.
PRIMARY_LAW: dict[str, dict[str, object]] = {
    "12012P": {
        "title": "Charter of Fundamental Rights of the European Union",
        "eli": "https://eur-lex.europa.eu/eli/treaty/char_2012/oj/eng",
        "aliases": ("12012P/TXT", "Charter of Fundamental Rights of the European Union",
                    "Charter of Fundamental Rights", "EU Charter", "the Charter"),
    },
    "12016M": {
        "title": "Consolidated version of the Treaty on European Union",
        "eli": "https://eur-lex.europa.eu/eli/treaty/teu_2016/oj/eng",
        "aliases": ("12016M/TXT", "Treaty on European Union", "TEU"),
    },
    "12016E": {
        "title": "Consolidated version of the Treaty on the Functioning of the European Union",
        "eli": "https://eur-lex.europa.eu/eli/treaty/tfeu_2016/oj/eng",
        "aliases": ("12016E/TXT", "Treaty on the Functioning of the European Union", "TFEU"),
    },
}

# Sector-1 (primary law) descriptors → the instrument the article belongs to.
_TREATY = {"E": "TFEU", "M": "TEU", "F": "TEU (pre-Lisbon)", "C": "EC Treaty",
           "A": "Euratom Treaty", "P": "Charter of Fundamental Rights", "D": "EEA Agreement"}
_DESC_KIND = {"R": "Regulation", "L": "Directive", "D": "Decision",
              "Q": "Institutional act", "M": "Other act"}
# Titles the EUR-Lex HTML gives that are NOT real titles (just the CELEX, an OJ
# filename, or a stray heading like "ANNEX").
_GENERIC_TITLE = re.compile(r"^\s*(?:EUR-Lex\b.*|ANNEX|[A-Z]_\d.*\.xml|)\s*$", re.IGNORECASE)


def celex_title(celex: str) -> str | None:
    """A human title derived from a CELEX when the source gives none: treaty/Charter
    articles → "Article 267 TFEU"; secondary law → "Regulation 2016/679"."""
    celex = celex.upper().removesuffix("/TXT")
    if celex in PRIMARY_LAW:
        return str(PRIMARY_LAW[celex]["title"])
    m = re.match(r"^(?P<sector>[1-9])(?P<year>\d{4})(?P<desc>[A-Z]{1,2})(?P<num>\d+)", celex)
    if not m:
        return None
    sector, year, desc, num = m.group("sector"), m.group("year"), m.group("desc"), m.group("num")
    if sector == "1":  # primary law: the number is the article number
        inst = _TREATY.get(desc[0], "EU primary law")
        return f"Article {int(num)} {inst}"
    kind = _DESC_KIND.get(desc[0])
    if kind:
        # directives are cited year/number, regulations number/year (pre-2015) — show
        # the colloquial order so it reads like a normal citation
        a, b = (year, str(int(num))) if (desc[0] == "L" or int(year) >= 2015) else (str(int(num)), year)
        return f"{kind} {a}/{b}"
    return None


def _is_generic_title(t: str | None) -> bool:
    return not t or bool(_GENERIC_TITLE.match(t))


def _year_span(spec: str | None) -> tuple[int, int] | None:
    """"1990-2026" or "2020" → (start, end); None if unset."""
    if not spec:
        return None
    m = re.match(r"^\s*(\d{4})\s*(?:-\s*(\d{4}))?\s*$", str(spec))
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return (min(a, b), max(a, b))

class EULegislationAdapter(BaseAdapter):
    source = "eu-legislation"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, celex: str | tuple[str, ...] | None = None,
                 types: str | None = None, years: str | None = None,
                 page_size: int = 200, client: RateLimitedClient | None = None) -> None:
        if isinstance(celex, str):
            celex = tuple(c.strip() for c in celex.split(",") if c.strip())
        self.celex_list = tuple(celex) if celex else ()
        self.types = tuple(t.strip().upper() for t in (types or "").split(",") if t.strip()) \
            or DEFAULT_TYPES
        self.years = _year_span(years)
        self.page_size = max(1, min(int(page_size), 1000))
        # With no explicit CELEX list, enumerate the catalogue over SPARQL.
        self.enumerate = not self.celex_list
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.enumerate:
            yield from self._discover_enumerate(since, max_pages=max_pages)
            return
        for celex in self.celex_list:
            celex = celex.upper().removesuffix("/TXT")
            primary = PRIMARY_LAW.get(celex)
            yield Stub(
                stable_id=celex,
                landing_url=str(primary["eli"]) if primary else
                            f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                raw_url=f"{CELEX_BASE}/{celex}",
                court=None,
            )

    # -- SPARQL enumeration (the full-catalogue path) -----------------------
    def _sparql(self, query: str) -> list[dict]:
        resp = self._client.request(
            "POST", SPARQL_ENDPOINT, data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        bindings = resp.json().get("results", {}).get("bindings", [])
        return [{k: v["value"] for k, v in row.items()} for row in bindings]

    def _enumerate_query(self, since: str | None, offset: int) -> str:
        descriptors = "".join(self.types)
        filters = [f'REGEX(STR(?celex), "^3[0-9]{{4}}[{descriptors}][0-9]{{4}}$")']
        if since:
            filters.append(f'STR(?date) > "{since[:10]}"')
        if self.years:
            filters.append(f'STR(?date) >= "{self.years[0]}-01-01"')
            filters.append(f'STR(?date) <= "{self.years[1]}-12-31"')
        where = " && ".join(filters)
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?celex ?date WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
  FILTER({where})
}}
ORDER BY DESC(?date)
LIMIT {self.page_size} OFFSET {offset}
"""

    def _discover_enumerate(self, since: str | None, *, max_pages: int | None) -> Iterator[Stub]:
        """Walk sector-3 legal acts newest-first. Incremental runs filter on the stored
        document-date cursor; a backfill pages with OFFSET until the series runs out."""
        offset = 0
        pages = 0
        seen: set[str] = set()
        while True:
            try:
                rows = self._sparql(self._enumerate_query(since, offset))
            except Exception:
                return  # a SPARQL hiccup ends the crawl; the cursor doesn't advance past it
            if not rows:
                return
            for row in rows:
                celex = (row.get("celex") or "").strip()
                if not celex or celex in seen:
                    continue
                seen.add(celex)
                yield Stub(
                    stable_id=celex,
                    landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                    raw_url=f"{CELEX_BASE}/{celex}",
                    hints={"watermark": row.get("date")} if row.get("date") else {},
                )
            pages += 1
            offset += len(rows)
            if len(rows) < self.page_size or (max_pages is not None and pages >= max_pages):
                return

    def _transposition_edges(self, celex: str) -> list[TypedRelation]:
        """`transposes` edges to national implementing measures — Directives only, and
        best-effort (a SPARQL hiccup must never fail the fetch of the directive itself)."""
        if not _DIRECTIVE_RE.match(celex or ""):
            return []
        from .eu_cellar import national_transposition_edges
        try:
            return national_transposition_edges(celex, self._sparql)
        except Exception:  # noqa: BLE001 — best-effort enrichment
            return []

    def fetch(self, stub: Stub) -> Record | None:
        primary = PRIMARY_LAW.get(stub.stable_id.upper())
        aliases = list(primary["aliases"]) if primary else []
        trans = self._transposition_edges(stub.stable_id)
        # Try Formex; a 404/FetchError (no Formex rendition) is NOT fatal — fall
        # through to the EUR-Lex HTML below rather than giving up.
        raw = None
        try:
            resp = self._client.get(
                stub.raw_url,
                headers={"Accept": "application/zip;mtype=fmx4", "Accept-Language": "eng"},
            )
            if getattr(resp, "status_code", 200) < 400:
                raw = resp.content
        except FetchError:
            raw = None
        parsed = parse("formex-legislation", raw) if raw else None
        if parsed and parsed.text:
            return Record(
                source=self.source,
                stable_id=stub.stable_id,  # CELEX — the resolution target (§5b)
                doc_type=DocType.LEGISLATION,
                title=str(primary["title"]) if primary else (parsed.title or stub.stable_id),
                language="en", source_language="en",
                landing_url=stub.landing_url,
                raw_bytes=raw, raw_ext="zip",
                text=parsed.text, segments=parsed.segments, relations=parsed.relations + trans,
                extracted_via=ExtractedVia.STRUCTURED,
                extra={"format": "formex-legislation", "celex": stub.stable_id,
                       "eli": primary.get("eli") if primary else None, "aliases": aliases},
            )
        # No Formex rendition (common for old instruments like Directive 95/46) — fall
        # back to the EUR-Lex HTML, which exists even when Formex doesn't.
        html = self._fetch_html(stub.stable_id)
        hp = parse("eurlex-html", html) if html else None
        if hp and hp.text:
            # the EUR-Lex HTML <title> is often generic ("EUR-Lex - 12008E267 - EN")
            # or a stray heading ("ANNEX") — derive a real title from the CELEX then.
            title = (str(primary["title"]) if primary else
                     celex_title(stub.stable_id) if _is_generic_title(hp.title) else hp.title)
            return Record(
                source=self.source, stable_id=stub.stable_id, doc_type=DocType.LEGISLATION,
                title=title or stub.stable_id, language="en", source_language="en",
                landing_url=stub.landing_url, raw_bytes=html, raw_ext="html",
                text=hp.text, segments=hp.segments, relations=hp.relations + trans,
                extracted_via=ExtractedVia.STRUCTURED,
                extra={"format": "eurlex-html", "celex": stub.stable_id,
                       "eli": primary.get("eli") if primary else None, "aliases": aliases},
            )
        # Neither Formex nor HTML parsed — register a metadata stub so the (often
        # heavily-cited) instrument is still a real, clickable node and its citations
        # resolve (§5b); text can be backfilled later.
        return Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.LEGISLATION,
            title=str(primary["title"]) if primary else stub.stable_id,
            language="en", source_language="en",
            landing_url=stub.landing_url, raw_bytes=stub.stable_id.encode(), raw_ext="txt",
            relations=trans,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={"celex": stub.stable_id, "metadata_only": True,
                   "eli": primary.get("eli") if primary else None, "aliases": aliases},
        )

    def _fetch_html(self, celex: str) -> bytes | None:
        """The EUR-Lex rendered HTML for a CELEX (the fallback when no Formex)."""
        url = f"https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:{celex}"
        try:
            r = self._client.get(url, headers={"Accept-Language": "eng"})
        except FetchError:
            return None
        return r.content if getattr(r, "status_code", 200) < 400 else None
