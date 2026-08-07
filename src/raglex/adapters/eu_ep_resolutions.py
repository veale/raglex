"""European Parliament adopted texts — resolutions, back to the first elected term.

A Parliament resolution is the EU's most sustained running commentary on law that is
already in force. An own-initiative resolution ("on the implementation of the rule of
law conditionality regime", "with recommendations to the Commission on Civil Law Rules
on Robotics") reads an instrument, takes a position on how it is working, and cites
the acts, the Court's case law and the Commission's practice by name for fifty or sixty
paragraphs. That is what makes it worth holding beside the instruments it discusses —
and it is why the default is the **IP** family (non-legislative resolutions) rather
than the whole adopted-texts output.

**Two services, and the choice between them is about time, not quality.**

*CELLAR* (this adapter's discovery) is the only route with depth: sector 5 descriptor
``IP`` runs from ``51979IP0839`` — the first directly-elected Parliament — through
today, 11,684 works, with 10,373 more under ``AP`` (legislative resolutions and
first-reading positions). But a resolution reaches CELLAR only when the OJ C issue
carrying it is published, which is routinely a year late: the February 2017 robotics
resolution appeared in OJ C 252 of 18 July 2018.

*The Parliament's own Open Data API* has the text within days of the vote, and richer
metadata besides (the committee report adopted, EuroVoc subjects, the sitting) — but
only from the 8th term. Its whole holding is 5,461 adopted texts from 2014.

So the adapter discovers over CELLAR and, when a document carries the Parliament's own
``P8_TA(2017)0051`` identifier, prefers the portal's SDOCTA rendition for the text.
Recent resolutions therefore arrive complete instead of waiting a year for the OJ, and
the 1979–2013 tail still arrives.

**Text availability degrades with age, and the adapter says so rather than pretending.**
Sampling the renditions by year: Formex from roughly 2007; EUR-Lex HTML for 1995–2006;
PDF only for parts of 1997–2004; and nothing machine-readable before about 1994, where
CELLAR holds the work but no expression of it. Those early records are still stored, as
metadata nodes: a resolution the corpus cannot read is still a resolution the corpus can
resolve a citation *to*, and the OJ reference is on it for a human to follow.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterator

from ..core.errors import FetchError
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
from ..formats.base import ParsedDoc
from .eu_legislation import CDM, CELEX_BASE, EULegislationAdapter, _is_generic_title

#: Sector-5 descriptors for acts of the Parliament.
#:
#: ``IP`` non-legislative resolutions — own-initiative, implementation, annual and
#: topical resolutions. This is the commentary, and the default.
#: ``AP`` legislative resolutions and first-reading positions. Included on request:
#: many are a single paragraph approving a proposal, and the substantive text is the
#: annexed position, so they add bulk out of proportion to their content.
#: ``DP`` decisions (discharge, immunity), ``BP`` budget, ``XP`` other.
FAMILIES = {
    "IP": ("resolutions", "Non-legislative resolutions (own-initiative, implementation)"),
    "AP": ("legislative-resolutions", "Legislative resolutions and first-reading positions"),
    "DP": ("decisions", "Decisions of the European Parliament"),
    "BP": ("budget", "Budgetary resolutions and decisions"),
    "XP": ("other", "Other acts of the European Parliament"),
}
DEFAULT_TYPES = ("IP",)

_CELEX_RE = re.compile(r"^5(\d{4})(IP|AP|DP|BP|XP)(\d+)$", re.I)
#: ``immc:P8_TA(2017)0051`` as CELLAR stores it in ``work_id_document``.
_IMMC_TA = re.compile(r"immc:P(\d{1,2})_TA(?:-PROV)?\((\d{4})\)(\d{3,4})", re.I)

EP_API = "https://data.europarl.europa.eu/api/v2"
EP_DISTRIBUTION = "https://data.europarl.europa.eu"


def family(celex: str) -> tuple[str, str]:
    m = _CELEX_RE.match(celex or "")
    return FAMILIES.get(m.group(2).upper() if m else "", ("other", "Other acts of the "
                                                          "European Parliament"))


def ta_reference(immc: str | None) -> str | None:
    """``immc:P8_TA(2017)0051`` → ``P8_TA(2017)0051``."""
    m = _IMMC_TA.search(immc or "")
    return f"P{m.group(1)}_TA({m.group(2)}){m.group(3)}" if m else None


def portal_id(ta: str | None) -> str | None:
    """``P8_TA(2017)0051`` → ``TA-8-2017-0051``, the Open Data API's document id."""
    m = _IMMC_TA.search(f"immc:{ta}" if ta else "")
    return f"TA-{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


#: How a resolution's own title opens, wherever it is printed.
_TITLE_HEAD = re.compile(
    r"^European Parliament\s+(?:legislative\s+|non-legislative\s+)?"
    r"(?:resolution|decision|position|recommendation|declaration)\b", re.I)
#: Where the header stops and the document begins. Without these the title ran on
#: through the OJ reference and into the first visa.
_TITLE_STOP = re.compile(
    r"^(?:The European Parliament\b|Official Journal\b|OJ [CL] \d|P\d{1,2}_TA\(|"
    r"[-–—]\s|\(\d{4}/\d{4}\([A-Z]+\)\)$)", re.I)
#: The Parliament reference as the OJ prints it inside the text itself.
_TA_IN_TEXT = re.compile(r"\bP(\d{1,2})_TA(?:-PROV)?\((\d{4})\)(\d{3,4})\b")


def title_from_text(text: str | None) -> str | None:
    """The title read off the document's own first page.

    The pre-2007 route is EUR-Lex's legacy HTML, whose ``<title>`` is the boilerplate
    "EUR-Lex - 52005IP0005 - EN" — no use to a reader, and no use to the rule that
    decides whether a resolution is about exactly one instrument. The real title is a
    line near the top and always opens the same way; a PDF wraps it over several lines,
    so continuation lines are joined until the header ends."""
    lines = [" ".join(x.split()) for x in (text or "")[:6000].splitlines()]
    lines = [x for x in lines if x][:60]
    for i, line in enumerate(lines):
        if not _TITLE_HEAD.match(line):
            continue
        parts = [line]
        for nxt in lines[i + 1:i + 5]:
            if _TITLE_STOP.match(nxt) or _TITLE_HEAD.match(nxt):
                break
            parts.append(nxt)
            if len(" ".join(parts)) >= 250:
                break
        return " ".join(parts)[:300].strip(" .,;")
    return None


#: "European Parliament resolution of 18 December 2025 on …" — the vote date, printed
#: in the title of every resolution in every rendition.
_DATE_IN_TITLE = re.compile(
    r"\bof\s+(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{4})\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}


def resolution_date(watermark: str | None, title: str | None) -> date | None:
    """The date the resolution was adopted.

    It matters more here than the ladder in ``effective_date`` can supply: a CELEX like
    ``52025IP0256`` carries a year but no ``/YYYY/`` path segment, so the identifier rung
    never fires and every resolution sorted as undated. CELLAR's ``work_date_document``
    is the vote date; where it is missing the title says it in words, because that is how
    a resolution is cited — "resolution of 18 December 2025"."""
    if watermark:
        try:
            return date.fromisoformat(str(watermark)[:10])
        except ValueError:
            pass
    m = _DATE_IN_TITLE.search(title or "")
    if m:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    return None


def ta_from_text(text: str | None) -> str | None:
    """The Parliament reference printed in the body — the only place it appears for the
    older resolutions, where CELLAR records no ``work_id_document``. Without it a
    citation to ``P6_TA(2005)0005`` has nothing to resolve against."""
    m = _TA_IN_TEXT.search((text or "")[:4000])
    return f"P{int(m.group(1))}_TA({m.group(2)}){m.group(3)}" if m else None


def aliases_for(celex: str, ta: str | None) -> list[str]:
    """Every form a resolution is cited by.

    Parliament cites its own texts as ``P8_TA(2017)0051``; the Official Journal and the
    Legislative Observatory use ``T8-0051/2017``; EUR-Lex uses the CELEX. All three are
    the same document, and a corpus that holds it under only one of them fails to
    resolve the other two."""
    out = [celex.upper()]
    m = _IMMC_TA.search(f"immc:{ta}" if ta else "")
    if m:
        term, year, number = m.group(1), m.group(2), m.group(3)
        out += [f"P{term}_TA({year}){number}", f"P{term}_TA-PROV({year}){number}",
                f"T{term}-{number}/{year}"]
    return list(dict.fromkeys(out))


class EPResolutionsAdapter(EULegislationAdapter):
    """CELLAR discovery over sector-5 EP acts, with the Parliament's portal preferred
    for the text of anything it holds."""

    source = "eu-ep-resolutions"
    min_interval = 0.5

    def __init__(self, *, celex=None, types: str | None = None, years: str | None = None,
                 use_ep_portal: bool | str = True, page_size: int = 200,
                 start_offset: int = 0, client=None) -> None:
        super().__init__(celex=celex, types=types or ",".join(DEFAULT_TYPES),
                         years=years, page_size=page_size, start_offset=start_offset,
                         client=client)
        self.use_ep_portal = str(use_ep_portal).strip().lower() not in ("false", "0", "no", "")

    # ---- discovery ---------------------------------------------------------------

    def _enumerate_query(self, since: str | None, offset: int) -> str:
        desc = "|".join(re.escape(t) for t in self.types if t in FAMILIES) or "IP"
        filters = []
        if since:
            filters.append(f'STR(?date) > "{since[:10]}"')
        if self.years:
            filters += [f'STR(?date) >= "{self.years[0]}-01-01"',
                        f'STR(?date) <= "{self.years[1]}-12-31"']
        where = " && ".join(filters)
        # GROUP BY collapses the per-language expression rows; work_id_document carries
        # both a "celex:…" self-reference and the "immc:P9_TA(…)" identifier, so both are
        # concatenated and the TA form is picked out in Python rather than by a fragile
        # SPARQL string filter.
        return f"""
PREFIX cdm: <{CDM}>
SELECT ?celex ?date (SAMPLE(?title0) AS ?title)
       (GROUP_CONCAT(DISTINCT STR(?docid0); separator="|") AS ?docids)
       (SAMPLE(?term0) AS ?term) WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER(REGEX(STR(?celex), "^5[0-9]{{4}}({desc})[0-9]+$"))
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
  OPTIONAL {{ ?work cdm:work_id_document ?docid0 }}
  OPTIONAL {{ ?work cdm:term_parliamentary ?term0 }}
  OPTIONAL {{ ?work cdm:work_has_expression ?exp .
              ?exp cdm:expression_uses_language ?lang . FILTER(STRENDS(STR(?lang), "/ENG"))
              ?exp cdm:expression_title ?title0 }}
  {f'FILTER({where})' if where else ''}
}}
GROUP BY ?celex ?date
ORDER BY DESC(?date)
LIMIT {self.page_size} OFFSET {offset}
"""

    def _discover_enumerate(self, since: str | None, *, max_pages: int | None) -> Iterator[Stub]:
        """Newest first, so an incremental run stops at its cursor.

        The offset is emitted as ``resume_offset`` and accepted back through
        ``start_offset``, so a backfill interrupted 9,000 resolutions in resumes there
        rather than re-walking a 22,000-work catalogue."""
        offset = self.start_offset
        pages = 0
        seen: set[str] = set()
        while True:
            try:
                rows = self._sparql(self._enumerate_query(since, offset))
            except Exception:  # noqa: BLE001 — a SPARQL hiccup ends the page walk
                return
            if not rows:
                return
            for row in rows:
                celex = (row.get("celex") or "").strip().upper()
                if not celex or celex in seen:
                    continue
                seen.add(celex)
                ta = next((x for x in (ta_reference(v)
                                       for v in (row.get("docids") or "").split("|")) if x), None)
                yield Stub(
                    stable_id=celex,
                    landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                    raw_url=f"{CELEX_BASE}/{celex}",
                    hints={"watermark": row.get("date"), "title": row.get("title"),
                           "ta_reference": ta, "parliamentary_term": row.get("term"),
                           "resume_offset": offset},
                )
            pages += 1
            offset += len(rows)
            if len(rows) < self.page_size or (max_pages is not None and pages >= max_pages):
                return

    def _target_metadata(self, celex: str) -> dict:
        """Title and TA reference for one explicitly named CELEX."""
        query = f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?title ?docid ?date WHERE {{
  ?work cdm:resource_legal_id_celex ?id . FILTER(STR(?id) = "{celex}")
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
  OPTIONAL {{ ?work cdm:work_id_document ?docid }}
  OPTIONAL {{ ?work cdm:work_has_expression ?exp .
              ?exp cdm:expression_uses_language ?lang . FILTER(STRENDS(STR(?lang), "/ENG"))
              ?exp cdm:expression_title ?title }}
}}"""
        out: dict = {"title": None, "ta_reference": None, "watermark": None}
        try:
            rows = self._sparql(query)
        except Exception:  # noqa: BLE001 — metadata is enrichment; the fetch still runs
            return out
        for row in rows:
            out["title"] = out["title"] or row.get("title")
            out["watermark"] = out["watermark"] or row.get("date")
            out["ta_reference"] = out["ta_reference"] or ta_reference(row.get("docid"))
        return out

    # ---- the Parliament's own portal ---------------------------------------------

    def _portal_xml(self, ta: str | None) -> tuple[bytes, str] | None:
        """The SDOCTA rendition and its landing URL, or ``None``.

        The distribution path is NOT derivable from the document id — a 2017 text sits
        under ``distribution/reds_iPlTa_Itm/TA-8-2017-0051/TA-8-2017-0051-FNL_en.xml``
        and a 2025 one under ``distribution/doc/TA-10-2025-0343_en.xml``. Guessing it
        404s, so the record's own ``is_exemplified_by`` is read and followed."""
        doc_id = portal_id(ta)
        if not doc_id:
            return None
        try:
            resp = self._client.get(
                f"{EP_API}/adopted-texts/{doc_id}",
                params={"format": "application/ld+json", "language": "en"},
                headers={"Accept": "application/ld+json"})
            if getattr(resp, "status_code", 200) >= 400 or not (resp.content or b"").strip():
                return None
            data = resp.json()
        except (FetchError, ValueError):
            return None
        path = None
        for work in data.get("data") or []:
            for expression in work.get("is_realized_by") or []:
                if not str(expression.get("id") or "").endswith("/en"):
                    continue
                for manifestation in expression.get("is_embodied_by") or []:
                    href = str(manifestation.get("is_exemplified_by") or "")
                    if href.lower().endswith(".xml"):
                        path = href
                        break
        if not path:
            return None
        url = f"{EP_DISTRIBUTION}/{path.lstrip('/')}"
        try:
            resp = self._client.get(url)
        except FetchError:
            return None
        if getattr(resp, "status_code", 200) >= 400 or not (resp.content or b"").strip():
            return None
        return resp.content, url

    # ---- fetch --------------------------------------------------------------------

    def fetch(self, stub: Stub) -> Record | None:
        if "title" not in stub.hints:
            stub.hints.update(self._target_metadata(stub.stable_id))
        celex = stub.stable_id.upper()
        ta = stub.hints.get("ta_reference")

        raw: bytes | None = None
        raw_ext = fmt = None
        parsed = None
        landing = stub.landing_url

        # 1. the Parliament's own XML — the only route that has a recent resolution at all
        if self.use_ep_portal and ta:
            got = self._portal_xml(ta)
            if got:
                candidate = parse("ep-ta-xml", got[0])
                if candidate.text:
                    raw, raw_ext, fmt, parsed = got[0], "xml", "ep-ta-xml", candidate
                    landing = f"https://www.europarl.europa.eu/doceo/document/{portal_id(ta)}_EN.html"

        # 2. Formex, once the OJ C issue carrying it is published (roughly 2007 onwards)
        if parsed is None:
            blob = self._fetch_formex(stub.raw_url or f"{CELEX_BASE}/{celex}", "en")
            candidate = parse("formex-resolution", blob) if blob else None
            if candidate and candidate.text:
                raw, raw_ext, fmt, parsed = blob, "zip", "formex-resolution", candidate

        # 3. the rendered HTML CELLAR serves for 1995–2006
        if parsed is None:
            blob = self._fetch_html(celex, "en")
            candidate = parse("eurlex-html", blob) if blob else None
            if candidate and candidate.text:
                raw, raw_ext, fmt, parsed = blob, "html", "eurlex-html", candidate

        # 4. the OJ PDF — the only copy of many 1997–2004 resolutions
        if parsed is None:
            blob = self._pdf(celex)
            if blob:
                from ..extraction import extract_bytes

                extracted = extract_bytes(blob, ext="pdf", mime="application/pdf")
                if (extracted.text or "").strip():
                    raw, raw_ext, fmt = blob, "pdf", "pdf"
                    parsed = ParsedDoc(text=extracted.text)

        text = parsed.text if parsed else None
        # EUR-Lex's legacy HTML titles every page "EUR-Lex - 52005IP0005 - EN", and a PDF
        # has no title at all, so the document's own first line is the last resort before
        # falling back to the CELEX.
        title = (stub.hints.get("title") or (parsed.title if parsed else None)
                 or title_from_text(text) or celex)
        if _is_generic_title(title):
            title = title_from_text(text) or celex
        metadata = dict((parsed.metadata if parsed else None) or {})
        # CELLAR records no work_id_document for the older resolutions; the OJ prints the
        # reference in the body, and it is what a citation to them uses.
        ta = ta or metadata.get("ep_document_id") or ta_from_text(text)
        adopted = resolution_date(stub.hints.get("watermark"), title)

        extra: dict = {
            "celex": celex,
            "ep_family": family(celex)[0],
            "ta_reference": ta,
            "aliases": aliases_for(celex, ta),
            "parliamentary_term": stub.hints.get("parliamentary_term"),
            "format": fmt,
            **{k: v for k, v in metadata.items() if k in ("report", "rapporteurs")},
        }
        # A resolution "on the implementation of Directive 2011/83/EU" discusses exactly
        # one instrument, and its bare "Article 5" references belong to that instrument.
        # A resolution has no articles of its own, so unlike a Commission proposal there
        # is nothing of its own for the rule to mis-file.
        from .eu_consumer_guidance import title_default_instrument

        default_instrument = title_default_instrument(title)
        if default_instrument:
            extra["citation_default_instrument"] = default_instrument

        relations: list[TypedRelation] = list((parsed.relations if parsed else None) or [])
        report = metadata.get("report")
        if report:
            # The committee report the plenary adopted — the resolution's own working
            # paper. Dangling until (and unless) committee documents are harvested, which
            # is the §5b worklist doing its job rather than a defect.
            relations.append(TypedRelation(
                relationship_type=RelationshipType.RELATED_TO,
                raw_citation_string=f"Adopts report {report}", dst_id=report,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))

        if parsed is None:
            # Pre-1995: CELLAR has the work but no readable expression. Store the node so
            # the citation resolves and the reader is pointed at EUR-Lex, rather than
            # dropping a resolution the corpus can legitimately name.
            extra["metadata_only"] = True
            return Record(
                source=self.source, stable_id=celex, doc_type=DocType.PREPARATORY,
                title=title, language="en", source_language="en",
                decision_date=adopted,
                landing_url=landing, raw_bytes=celex.encode(), raw_ext="txt",
                relations=relations, extracted_via=ExtractedVia.STRUCTURED, extra=extra)

        return Record(
            source=self.source, stable_id=celex, doc_type=DocType.PREPARATORY,
            title=title, language="en", source_language="en",
            decision_date=adopted,
            landing_url=landing, raw_bytes=raw, raw_ext=raw_ext,
            text=parsed.text, segments=list(parsed.segments or []),
            relations=relations, extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["european-parliament", family(celex)[0]],
            extra=extra)

    def _pdf(self, celex: str) -> bytes | None:
        try:
            resp = self._client.get(f"{CELEX_BASE}/{celex}",
                                    headers={"Accept": "application/pdf",
                                             "Accept-Language": "eng"})
        except FetchError:
            return None
        content = resp.content or b""
        if getattr(resp, "status_code", 200) < 400 and content.startswith(b"%PDF"):
            return content
        return None
