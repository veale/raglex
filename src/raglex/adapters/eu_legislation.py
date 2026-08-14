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
``D``) newest-first, paging with ``OFFSET``. ``consolidations_only=true`` instead walks
the complete sector-0 dated-expression series — including future-effective snapshots.
An **incremental** run stops at the stored document-date cursor; a **backfill** (no
cursor, no page cap) walks the whole series. ``types=`` picks the descriptors,
``years=`` bounds the span.
"""

from __future__ import annotations

import re
import time
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

# A Directive CELEX (sector 3, descriptor L) — the only instruments that have national
# transposition measures, so the only ones we run the (extra) transposition query for.
_DIRECTIVE_RE = re.compile(r"^3\d{4}L\d", re.IGNORECASE)

CELEX_BASE = "https://publications.europa.eu/resource/celex"
SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
CDM = "http://publications.europa.eu/ontology/cdm#"
# Sector 3 = legal acts. R = Regulation, L = Directive, D = Decision — the legislative
# mass. The trailing anchor drops corrigenda (``32019L1153R(02)``), which are not
# separate instruments.
DEFAULT_TYPES = ("R", "L", "D", "TREATY")

# Consolidated EU primary-law documents. Citations use the CELEX stem (12016E),
# EUR-Lex displays /TXT, and ELI supplies the durable web identity. All forms and
# ordinary legal names must converge on the same held node.
PRIMARY_LAW: dict[str, dict[str, object]] = {
    "12012P": {
        "title": "Charter of Fundamental Rights of the European Union",
        "eli": "https://eur-lex.europa.eu/eli/treaty/char_2012/oj/eng",
        "aliases": ("12012P/TXT", "Charter of Fundamental Rights of the European Union",
                    "Charter of Fundamental Rights", "EU Charter", "CFREU"),
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


def _flag(value: bool | str | None) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "on"}

class EULegislationAdapter(BaseAdapter):
    source = "eu-legislation"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, celex: str | tuple[str, ...] | None = None,
                 types: str | None = None, years: str | None = None,
                 include_consolidations: bool | str = False,
                 consolidations_only: bool | str = False,
                 start_offset: int = 0,
                 page_size: int = 200, client: RateLimitedClient | None = None) -> None:
        if isinstance(celex, str):
            celex = tuple(c.strip() for c in celex.split(",") if c.strip())
        self.celex_list = tuple(celex) if celex else ()
        self.types = tuple(t.strip().upper() for t in (types or "").split(",") if t.strip()) \
            or DEFAULT_TYPES
        self.years = _year_span(years)
        self.include_consolidations = _flag(include_consolidations)
        self.consolidations_only = _flag(consolidations_only)
        self.start_offset = max(0, int(start_offset or 0))
        self.page_size = max(1, min(int(page_size), 1000))
        # With no explicit CELEX list, enumerate the catalogue over SPARQL.
        self.enumerate = not self.celex_list
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.consolidations_only:
            yield from self._discover_consolidations(max_pages=max_pages)
            return
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
            if self.include_consolidations and re.match(r"^3\d{4}[RLD]\d{4}$", celex):
                yield from self._consolidation_stubs(celex)

    def _consolidation_stubs(self, base_celex: str) -> Iterator[Stub]:
        """All dated sector-0 snapshots for one explicitly named base act."""
        prefix = "0" + base_celex[1:] + "-"
        query = f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?celex WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER(STRSTARTS(STR(?celex), "{prefix}"))
}}
ORDER BY ?celex
"""
        try:
            rows = self._sparql(query)
        except Exception:
            return
        total = len(rows) + 1  # the base stub precedes these dated expressions
        for position, row in enumerate(rows, 2):
            celex = str(row.get("celex") or "").strip().upper()
            if not re.fullmatch(r"0\d{4}[RLD]\d{4}-\d{8}", celex):
                continue
            yield Stub(
                stable_id=celex,
                landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                raw_url=f"{CELEX_BASE}/{celex}",
                hints={
                    "consolidation_of": base_celex,
                    "feed_total": total,
                    "feed_position": position,
                },
            )

    # -- SPARQL enumeration (the full-catalogue path) -----------------------
    def _sparql(self, query: str) -> list[dict]:
        resp = self._client.request(
            "POST", SPARQL_ENDPOINT, data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        bindings = resp.json().get("results", {}).get("bindings", [])
        return [{k: v["value"] for k, v in row.items()} for row in bindings]

    def _consolidation_query(self, offset: int) -> str:
        """Enumerate every dated sector-0 expression.

        Do not filter against today's date: EUR-Lex publishes future-effective
        consolidations in advance, and RagLex must hold them while keeping them
        distinct from the latest snapshot applicable *today*.
        """
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?celex WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
        FILTER(REGEX(STR(?celex), "^0[0-9]{{4}}[A-Z]+[0-9]+-[0-9]{{8}}$"))
}}
ORDER BY ?celex
LIMIT {self.page_size} OFFSET {offset}
"""

    def _discover_consolidations(self, *, max_pages: int | None) -> Iterator[Stub]:
        """Walk the complete Cellar consolidation catalogue with a durable offset.

        Each distinct sector-3 base is yielded immediately before the first sector-0
        expression that reveals it. Consolidations omit the preamble, so a reverse-only
        walk that stored just sector 0 could never expose the original recitals. The
        pipeline deduplicates an already-held base cheaply; a one-time repair harvest can
        use ``refetch_held`` to upgrade old flattened-HTML renditions to Formex.
        """
        offset = self.start_offset
        pages = 0
        yielded_bases: set[str] = set()
        while True:
            try:
                rows = self._sparql(self._consolidation_query(offset))
            except Exception:
                return
            if not rows:
                return
            # Sector 0 replaces (and therefore hides) the base act's sector digit.
            # Resolve it in one batched VALUES query per page across the only base
            # sectors EUR-Lex consolidates: treaties, international agreements,
            # secondary law and complementary legislation.
            bodies = {
                str(row.get("celex") or "")[1:].split("-", 1)[0]
                for row in rows
                if re.fullmatch(
                    r"0\d{4}[A-Z]+\d+-\d{8}",
                    str(row.get("celex") or "").strip().upper(),
                )
            }
            candidates = [f"{sector}{body}" for body in bodies for sector in "1234"]
            bases: set[str] = set()
            if candidates:
                values = " ".join(f'"{candidate}"' for candidate in candidates)
                try:
                    base_rows = self._sparql(f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?base WHERE {{
  VALUES ?base {{ {values} }}
  ?work cdm:resource_legal_id_celex ?base .
}}
""")
                    bases = {
                        str(row.get("base") or "").strip().upper()
                        for row in base_rows
                    }
                except Exception:
                    bases = set()
            for index, row in enumerate(rows, 1):
                celex = str(row.get("celex") or "").strip().upper()
                if not re.fullmatch(r"0\d{4}[A-Z]+\d+-\d{8}", celex):
                    continue
                body = celex[1:].split("-", 1)[0]
                matching = [candidate for candidate in bases if candidate[1:] == body]
                base = (
                    next((candidate for candidate in matching if candidate.startswith("3")), None)
                    or (matching[0] if len(matching) == 1 else None)
                    or "3" + body
                )
                if base not in yielded_bases:
                    yielded_bases.add(base)
                    yield Stub(
                        stable_id=base,
                        landing_url=(
                            "https://eur-lex.europa.eu/legal-content/EN/ALL/"
                            f"?uri=CELEX:{base}"
                        ),
                        raw_url=f"{CELEX_BASE}/{base}",
                        hints={
                            "from_consolidation_sweep": True,
                            "consolidation_revealed_by": celex,
                        },
                    )
                yield Stub(
                    stable_id=celex,
                    landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                    raw_url=f"{CELEX_BASE}/{celex}",
                    hints={
                        "consolidation_of": base,
                        # Some historical expressions genuinely publish metadata but no
                        # English body. Once that result is held, the weekly complete
                        # sweep must not download it forever; an explicit refetch repair
                        # can still override the pipeline prefilter.
                        "metadata_only_complete": True,
                        # Pipeline persists this on the harvest job. A deploy resumes
                        # at the next row rather than replaying the whole Cellar walk.
                        "resume_offset": offset + index,
                    },
                )
            pages += 1
            offset += len(rows)
            if len(rows) < self.page_size or (max_pages is not None and pages >= max_pages):
                return

    def _enumerate_query(self, since: str | None, offset: int) -> str:
        descriptors = "".join(t for t in self.types if len(t) == 1)
        branches = []
        if descriptors:
            branches.append(f'{{ ?work cdm:resource_legal_id_celex ?celex . '
                            f'FILTER(REGEX(STR(?celex), "^3[0-9]{{4}}[{descriptors}][0-9]{{4}}$")) }}')
        if "TREATY" in self.types:
            # Public CELLAR equivalent of EUR-Lex expert-search
            # ``PS_ID=treaty OR FM_CODED=TREATY``. Whole instruments have a sector-1
            # CELEX ending at the descriptor; article fragments continue with digits.
            branches.append('{ { ?work a cdm:treaty } UNION '
                            '{ ?work cdm:work_has_resource-type '
                            '<http://publications.europa.eu/resource/authority/resource-type/TREATY> } '
                            '?work cdm:resource_legal_id_celex ?celex . '
                            'FILTER(REGEX(STR(?celex), "^1[0-9]{4}[A-Z]{1,2}$")) }')
        filters = []
        if since:
            filters.append(f'STR(?date) > "{since[:10]}"')
        if self.years:
            filters.append(f'STR(?date) >= "{self.years[0]}-01-01"')
            filters.append(f'STR(?date) <= "{self.years[1]}-12-31"')
        where = " && ".join(filters)
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?celex ?date WHERE {{
  {' UNION '.join(branches)}
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
  {f'FILTER({where})' if where else ''}
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
        # English first, then French. EUR-Lex sometimes serves the only available
        # French expression even under an EN URL, so inspect the body rather than
        # trusting the request path's language label.
        from .eu_cellar import _rendition_language

        fallback = None
        chosen = None
        for language in ("en", "fr"):
            raw = self._fetch_formex(stub.raw_url, language)
            parsed = parse("formex-legislation", raw) if raw else None
            raw_ext = "zip"
            if not parsed or not parsed.text:
                # A treaty is addressed by its CELEX STEM (12016E) but EUR-Lex only
                # serves the text under the /TXT expression. The stem alone answers with
                # the Official Journal's front matter — the C 202 table of contents —
                # which extracts to the numerals 1 to 30 and nothing else. Both treaties
                # sat like that under 239,151 incoming edges: every "Article 267 TFEU" in
                # the corpus resolved to a document with no Article 267 in it.
                raw = self._fetch_html(
                    f"{stub.stable_id}/TXT" if primary else stub.stable_id, language)
                parsed = parse("eurlex-html", raw) if raw else None
                raw_ext = "html"
            if not parsed or not parsed.text:
                continue
            detected = _rendition_language(parsed.text)
            candidate = (raw, raw_ext, parsed, detected or language)
            if language == "en" and detected == "fr":
                fallback = candidate
                continue
            chosen = candidate
            break
        if chosen is None:
            chosen = fallback

        if chosen:
            raw, raw_ext, parsed, source_language = chosen
            is_formex = raw_ext == "zip"
            # the EUR-Lex HTML <title> is often generic ("EUR-Lex - 12008E267 - EN")
            # or a stray heading ("ANNEX") — derive a real title from the CELEX then.
            title = (str(primary["title"]) if primary else
                     (parsed.title or stub.stable_id) if is_formex else
                     celex_title(stub.stable_id) if _is_generic_title(parsed.title)
                     else parsed.title)
            return self._decorate_currency(Record(
                source=self.source,
                stable_id=stub.stable_id,  # CELEX — the resolution target (§5b)
                doc_type=DocType.LEGISLATION,
                title=title or stub.stable_id,
                language=source_language, source_language=source_language,
                landing_url=stub.landing_url,
                raw_bytes=raw, raw_ext=raw_ext,
                text=parsed.text, segments=parsed.segments, relations=parsed.relations + trans,
                extracted_via=ExtractedVia.STRUCTURED,
                extra={"format": "formex-legislation" if is_formex else "eurlex-html",
                       "celex": stub.stable_id,
                       "eli": primary.get("eli") if primary else None, "aliases": aliases,
                       **({"language_fallback": "en-to-fr"}
                          if source_language == "fr" else {})},
            ), base_celex=stub.hints.get("consolidation_of"))
        # Neither Formex nor HTML parsed — register a metadata stub so the (often
        # heavily-cited) instrument is still a real, clickable node and its citations
        # resolve (§5b); text can be backfilled later.
        return self._decorate_currency(Record(
            source=self.source, stable_id=stub.stable_id, doc_type=DocType.LEGISLATION,
            title=str(primary["title"]) if primary else stub.stable_id,
            language="en", source_language="en",
            landing_url=stub.landing_url, raw_bytes=stub.stable_id.encode(), raw_ext="txt",
            relations=trans,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={"celex": stub.stable_id, "metadata_only": True,
                   "eli": primary.get("eli") if primary else None, "aliases": aliases},
        ), base_celex=stub.hints.get("consolidation_of"))

    @staticmethod
    def _decorate_currency(record: Record, *, base_celex: str | None = None) -> Record:
        """Attach sector-0 identity and its authoritative base-act edge."""
        from ..eu_law import consolidation_base, consolidation_date

        base = base_celex or consolidation_base(record.stable_id)
        if not base:
            return record
        if not any(
            rel.relationship_type is RelationshipType.CONSOLIDATES
            and rel.dst_id == base for rel in record.relations
        ):
            record.relations.append(TypedRelation(
                relationship_type=RelationshipType.CONSOLIDATES,
                raw_citation_string=record.stable_id,
                dst_id=base,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))
        as_at = consolidation_date(record.stable_id)
        record.extra.update({
            "is_consolidation": True,
            "consolidation_of": base,
            "as_at": as_at,
            "future_effective": bool(as_at and as_at > date.today().isoformat()),
            "text_status": "consolidated (documentation only; no legal effect)",
            "is_authoritative": False,
        })
        return record

    def _fetch_formex(self, url: str, language: str = "en") -> bytes | None:
        try:
            resp = self._client.get(
                url,
                headers={"Accept": "application/zip;mtype=fmx4",
                         "Accept-Language": {"en": "eng", "fr": "fra"}.get(language, language)},
            )
            if getattr(resp, "status_code", 200) < 400:
                return resp.content
        except FetchError:
            pass
        return None

    def _fetch_html(self, celex: str, language: str = "en") -> bytes | None:
        """The rendered HTML for a CELEX (the fallback when no Formex): the EUR-Lex
        display page first, then CELLAR's own ``text/html`` rendition.

        EUR-Lex sits behind a WAF that answers automation with an EMPTY HTTP 202 — a
        "success" with no bytes, which the old ``< 400`` check accepted, so every
        pre-Formex instrument quietly became a metadata stub (Directive 70/156,
        cited constantly, sat as a bare CELEX for years). Anything but a 200 with a
        real body falls through to CELLAR at ``{CELEX_BASE}/{celex}``, which serves
        the same rendition unchallenged and is the only machine-reachable copy for
        the old instruments."""
        lang = language.upper()
        accept_language = {"en": "eng", "fr": "fra"}.get(language, language)
        url = f"https://eur-lex.europa.eu/legal-content/{lang}/TXT/HTML/?uri=CELEX:{celex}"
        # The 202 is not a refusal, it is CELLAR's content negotiation failing under
        # load: the body says "None of the requests returned successfully a redirection
        # … Invalid content type CONTENT_STREAM for WORK without language", and the same
        # request with the same headers succeeds moments later. Treating the first one as
        # final is what makes the failure look like a block; three tries, spaced, turns
        # it back into an ordinary fetch. An explicit Accept goes with the
        # Accept-Language, because the negotiation wants both.
        headers = {"Accept": "text/html,application/xhtml+xml",
                   "Accept-Language": f"{accept_language}, {language};q=0.9"}
        for attempt in range(3):
            try:
                r = self._client.get(url, headers=headers)
                if getattr(r, "status_code", 200) == 200 and len(r.content or b"") > 500:
                    return r.content
            except FetchError:
                break
            if attempt < 2:
                time.sleep(2 + 3 * attempt)
        try:
            r = self._client.get(f"{CELEX_BASE}/{celex}",
                                 headers={"Accept": "text/html",
                                          "Accept-Language": accept_language})
        except FetchError:
            return None
        if getattr(r, "status_code", 200) == 200 and (r.content or b"").strip():
            return r.content
        return None
