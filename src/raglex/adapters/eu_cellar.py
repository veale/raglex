"""European Union — CELLAR adapter (SPARQL discovery + Formex content).

CELLAR (the Publications Office repository) is the whole EU-law layer in one place
and, for this system, *both* a source and the **resolution target** for the CJEU
and regulation citations that everything else makes (§2, §9 step 6). It is the
canonical SPARQL adapter type (§1.6).

Two endpoints, no auth:
- **SPARQL** (`/webapi/rdf/sparql`, CDM ontology) for discovery + the citation
  graph — Rechtspraak-style "graph for free", but EU-wide.
- **REST content negotiation** (`/resource/celex/{CELEX}`) for the document; CJEU
  judgments are reliably available as **Formex 4** (zip-wrapped XML). The operative
  ruling lives in `<JURISDICTION>` (NOT `<DISPOSITIF>`, which is a *legislative*
  element); reasoning is in `<CONTENTS.JUDGMENT>`; paragraphs are `<NP.ECR>`.

This adapter discovers CJEU case law **relative to a named instrument or case**: set
`legislation_celex` to follow the case law on a piece of legislation, or
`cited_by_celex` to find judgments citing a given case. One of the two is required —
there is no default instrument. Each case yields a typed edge to that legislation
(`interprets`/`applies`/`overrules`) plus `mentions` edges to the cases it cites,
all with ECLI destinations so they resolve directly (§5b).

SPARQL query forms adapted from the working caselaw-mcp server (CDM ontology).
Parsing is split from HTTP (`unzip_formex` / `extract_formex_text` are pure).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    Segment,
    Stub,
    TypedRelation,
)
from ..core.segmentation import assemble, blocks_by_localname, element_text

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
CELEX_BASE = "https://publications.europa.eu/resource/celex"
CDM = "http://publications.europa.eu/ontology/cdm#"

GDPR_CELEX = "32016R0679"

# EUR-Lex Expert Search SOAP webservice (credentialed) — the authoritative source
# of a case's official title (EXPRESSION_TITLE), which the free CELLAR RDF omits.
# Quota-limited per day, so we BATCH: one call fetches the titles for many CELEXes
# via "DN = a OR DN = b OR …".
EURLEX_ENDPOINT = "https://eur-lex.europa.eu/EURLexWebService"
EURLEX_PAGE_SIZE = 50  # webservice max per page → our batch size
_EURLEX_SOAP = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:sear="http://eur-lex.europa.eu/search"
               xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
  <soap:Header>
    <wsse:Security soap:mustUnderstand="true"
        xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{username}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{password}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soap:Header>
  <soap:Body>
    <sear:searchRequest>
      <sear:expertQuery>{query}</sear:expertQuery>
      <sear:page>1</sear:page>
      <sear:pageSize>{page_size}</sear:pageSize>
      <sear:searchLanguage>en</sear:searchLanguage>
    </sear:searchRequest>
  </soap:Body>
</soap:Envelope>"""


# Content fields worth lifting from the webservice that the free CELLAR RDF lacks.
# The title becomes the document title; the rest become tags (subject classification).
_EURLEX_TITLE_FIELDS = ("EXPRESSION_TITLE", "RESOURCE_LEGAL_TITLE")
_EURLEX_SUBJECT_FIELDS = ("SUBJECT_MATTER", "RESOURCE_LEGAL_IS_ABOUT_CONCEPT_EUROVOC",
                          "CASE-LAW_IS_ABOUT_CONCEPT", "CASE-LAW_DIRECTORY_CODE",
                          "EUROVOC", "CLASSIFICATIONS_CODE")


def eurlex_metadata(celexes: list[str], *, username: str | None = None,
                    password: str | None = None,
                    max_consecutive_failures: int = 3) -> dict[str, dict]:
    """Augment a batch of CJEU cases from the authoritative EUR-Lex webservice with
    everything useful the free CELLAR RDF omits — the official **title** and the
    **subject-matter / EuroVoc** classification. **One credentialed call per ≤50
    ids** (quota-friendly). Returns ``{celex: {"title": str, "subjects": [str]}}``;
    empty if no creds or the call fails (best-effort, never raises)."""
    import os
    from xml.sax.saxutils import escape

    user = username or os.environ.get("EURLEX_USERNAME")
    pw = password or os.environ.get("EURLEX_PASSWORD")
    celexes = [c for c in dict.fromkeys(celexes) if c]
    if not (user and pw and celexes):
        return {}
    import httpx

    out: dict[str, dict] = {}
    # The webservice 500s for days at a time. Grinding every remaining chunk against a
    # dead endpoint just burns an hour of the scheduler's tick; give up after a few
    # consecutive failures and let the caller back off.
    consecutive_failures = 0
    for i in range(0, len(celexes), EURLEX_PAGE_SIZE):
        chunk = celexes[i: i + EURLEX_PAGE_SIZE]
        query = " OR ".join(f"DN = {c}" for c in chunk)
        body = _EURLEX_SOAP.format(username=escape(user.strip()), password=escape(pw.strip()),
                                   query=escape(query), page_size=len(chunk))
        try:
            resp = httpx.post(EURLEX_ENDPOINT, content=body.encode("utf-8"),
                              headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                              timeout=60)
            resp.raise_for_status()
            out.update(_parse_eurlex_metadata(resp.content))
            consecutive_failures = 0
        except Exception:  # noqa: BLE001 — best-effort enrichment
            consecutive_failures += 1
            if consecutive_failures >= max_consecutive_failures:
                break
            continue
    return out


def eurlex_titles(celexes: list[str], **kw) -> dict[str, str]:
    """Just the titles (back-compat / convenience over :func:`eurlex_metadata`)."""
    return {c: m["title"] for c, m in eurlex_metadata(celexes, **kw).items() if m.get("title")}


# CELEX/case-number in the EXPRESSION_TITLE: "Case C-311/18", "Joined Cases C-17/22
# and C-18/22", French "Affaire C-60/22".
_CASE_NO_RE = re.compile(
    r"(?:Joined\s+Cases?|Cases?|Affaires?)\s+([CTF][-‑]\d+/\d+(?:\s+(?:and|to|et|&)\s+[CTF][-‑]\d+/\d+)*)",
    re.IGNORECASE,
)
_TITLE_HEADER_RE = re.compile(
    r"^(Judgment|Order|Opinion|View|Arr[êe]t|Ordonnance|Avis|Conclusions|Urteil|Sentenza|Auto)\b",
    re.IGNORECASE,
)
_TRAILING_DOCKET_RE = re.compile(
    r"\s*\((?:(?:Joined\s+)?Cases?\s+)?[CTF][-‑–]?\d+/\d+(?:\s*(?:P|RX))?"
    r"(?:\s*(?:,|and|to|et|&)\s*[CTF][-‑–]?\d+/\d+(?:\s*(?:P|RX))?)*\)\s*$",
    re.IGNORECASE,
)


def clean_case_display_title(title: str | None) -> str | None:
    """Drop a terminal parenthesised C/T/F docket echo from a party-name title.
    The ECLI/CELEX already carries identity; ``OC (C-479/22P)`` should display as
    ``OC``. Covers Court, General Court, Civil Service and appeal/RX suffixes."""
    if not title:
        return title
    return _TRAILING_DOCKET_RE.sub("", title).strip()


def concise_case_title(raw: str) -> str:
    """Reduce a CJEU EXPRESSION_TITLE to the **party names + case number** —
    "ND v DR (C-21/23)" — dropping the court/date, the referring court, and the long
    subject-matter summary. Robust to the '#'-joined raw form and our '—'-joined
    stored form, and to EN/FR/DE titles."""
    if not raw:
        return raw
    parts = [p.strip().strip(".") for p in re.split(r"\s*#\s*|\s+—\s+", raw) if p.strip()]
    if not parts:
        return raw
    m = next((m for p in parts if (m := _CASE_NO_RE.search(p))), None)
    case_no = re.sub(r"\s+(?:and|to|et|&)\s+", ", ", m.group(1)).replace("‑", "-") if m else None
    # the parties are the segment right after the "Judgment of the Court …" header
    parties = parts[1] if (len(parts) >= 2 and _TITLE_HEADER_RE.match(parts[0])) else parts[0]
    parties = _CASE_NO_RE.sub("", parties)
    parties = re.sub(r"\s*\([CTF][-‑]\d+/\d+\)", "", parties).strip(" .—-")  # drop inline (C-…/…)
    if parties and case_no:
        return clean_case_display_title(f"{parties} ({case_no})") or parties
    return parties or (f"Case {case_no}" if case_no else raw)


def _parse_eurlex_metadata(xml: bytes) -> dict[str, dict]:
    """``{CELEX: {title, subjects}}`` from an Expert Search response — one entry per
    ``<result>``, keyed by the result's own CELEX/DN."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {}
    out: dict[str, dict] = {}
    for result in (e for e in root.iter() if _localname(e.tag) == "result"):
        celex, title, subjects = None, None, []
        for el in result.iter():
            ln = _localname(el.tag).upper()
            if ln in ("ID_CELEX", "DN") and celex is None:
                celex = _eurlex_value(el)
            elif ln in _EURLEX_TITLE_FIELDS and title is None:
                vals = _eurlex_values(el)
                # the full EXPRESSION_TITLE is court+date+parties+referring court+
                # the whole subject-matter summary; keep just parties + case number
                title = concise_case_title(vals[0]) if vals else None
            elif ln in _EURLEX_SUBJECT_FIELDS:
                subjects.extend(_eurlex_values(el))
        if celex and (title or subjects):
            out[celex] = {"title": title, "subjects": list(dict.fromkeys(s for s in subjects if s))}
    return out


def _eurlex_values(el: ET.Element) -> list[str]:
    vals = [v.text.strip() for v in el.iter()
            if _localname(v.tag).upper() == "VALUE" and (v.text or "").strip()]
    if not vals and (el.text or "").strip():
        vals = [el.text.strip()]
    return vals


def _eurlex_value(el: ET.Element) -> str | None:
    vals = _eurlex_values(el)
    return vals[0] if vals else None

# CJEU document types span more than judgments (§1.3 polymorphic doc model). The
# CELEX descriptor encodes both the court (1st letter after the year) and the
# instrument (2nd letter); the CDM resource-type, when present, is authoritative.
#   C* = Court of Justice · T* = General Court · F* = Civil Service Tribunal
#   *J = judgment · *O = order · *V = Opinion of the Court (e.g. Opinion 1/15,
#   Canada PNR) · *C / *A = Advocate General opinion/view
_COURT_BY_SECTOR = {
    "C": "Court of Justice",
    "T": "General Court",
    "F": "Civil Service Tribunal",
}
_DOCTYPE_BY_DESCRIPTOR = {
    "J": DocType.JUDGMENT,
    "O": DocType.DECISION,  # order
    "V": DocType.OPINION,  # Opinion of the Court
    "C": DocType.OPINION,  # AG opinion (conclusions)
    "A": DocType.OPINION,  # AG view
}
# CDM work_has_resource-type prefixes → doc_type (authoritative when available).
_RESOURCE_TYPE_DOCTYPE = {
    "JUDG": DocType.JUDGMENT,
    "ORDER": DocType.DECISION,
    "OPIN_JUR": DocType.OPINION,  # Opinion of the Court
    "OPIN_AG": DocType.OPINION,
    "VIEW": DocType.OPINION,
}


def classify_celex(celex: str | None, resource_type: str | None = None) -> tuple[DocType, str]:
    """Map a CJEU CELEX (+ optional CDM resource-type) to (doc_type, court).
    Falls back sensibly so an unrecognised descriptor still catalogues as a CoJ
    judgment rather than crashing."""
    court = "Court of Justice"
    doc_type = DocType.JUDGMENT
    m = re.match(r"^6\d{4}([CTF])([A-Z])", celex or "")
    if m:
        court = _COURT_BY_SECTOR.get(m.group(1), court)
        doc_type = _DOCTYPE_BY_DESCRIPTOR.get(m.group(2), DocType.JUDGMENT)
        if m.group(2) in ("C", "A"):  # AG opinion / view
            court = "Advocate General"
    if resource_type:
        rt = resource_type.upper()
        for prefix, dt in _RESOURCE_TYPE_DOCTYPE.items():
            if rt.startswith(prefix):
                doc_type = dt
                break
    return doc_type, court

# CELLAR legislation-link CDM properties → typed treatment (§1A). These are how a
# judgment *engages* a legislative act; the property name is the relationship.
_LEGISLATION_LINKS: dict[str, RelationshipType] = {
    "case-law_interpretes_resource_legal": RelationshipType.INTERPRETS,
    "case-law_confirms_resource_legal": RelationshipType.APPLIES,
    "case-law_declares_void_resource_legal": RelationshipType.OVERRULES,
    "case-law_declares_valid_resource_legal": RelationshipType.APPLIES,
    "case-law_requests_annulment_of_resource_legal": RelationshipType.CONSIDERS,
    "case-law_states_failure_concerning_resource_legal": RelationshipType.CONSIDERS,
}


def _parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


# National transposition measures (NIM/MNE) live in CELLAR's CDM graph and are reachable
# by SPARQL — this is a CELLAR feature, NOT a SOAP-only one. For a directive we pull the
# national measures that implement it and mint `transposes` edges (directive → national
# measure) whose destinations resolve against fr-legislation / de-neuris once they exist,
# turning "GDPR ⇐ transposed by ⇒ BDSG / loi Informatique et Libertés" into a live edge.
_NIM_ELI_RE = re.compile(r"(eli/[^\s?#\"']+)", re.IGNORECASE)


def _transposition_query(celex: str) -> str:
    return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?nim ?nimCelex ?country ?eli ?title WHERE {{
  ?dir cdm:resource_legal_id_celex ?dc . FILTER(STR(?dc) = "{celex}")
  ?nim cdm:resource_legal_implements_resource_legal ?dir .
  OPTIONAL {{ ?nim cdm:resource_legal_id_celex ?nimCelex }}
  OPTIONAL {{ ?nim cdm:resource_legal_in_force_country ?c .
             BIND(REPLACE(STR(?c), "^.*/", "") AS ?country) }}
  OPTIONAL {{ ?nim cdm:resource_legal_id_local ?eli }}
  OPTIONAL {{ ?nim cdm:work_has_expression ?e . ?e cdm:expression_title ?title }}
}}
LIMIT 500
"""


def national_transposition_edges(celex: str, sparql) -> list[TypedRelation]:
    """`transposes` edges from a directive CELEX to its national implementing measures,
    using a caller-supplied ``sparql(query) -> list[dict]``. Most destinations aren't in
    the corpus yet, so the edge is dangling (dst None) with the national title/country/ELI
    kept in ``raw_citation_string`` — it surfaces in the §5b worklist and resolves when
    fr-legislation / de-neuris harvests the measure. A national ELI, when present, is used
    directly as the destination id."""
    edges: list[TypedRelation] = []
    seen: set[str] = set()
    for row in sparql(_transposition_query(celex)):
        eli = row.get("eli") or ""
        m = _NIM_ELI_RE.search(eli)
        dst = m.group(1).rstrip("/") if m else None
        title = row.get("title") or row.get("nimCelex") or eli or row.get("nim")
        country = row.get("country")
        key = dst or f"{title}|{country}"
        if not title or key in seen:
            continue
        seen.add(key)
        raw = title if not country else f"{title} | country: {country}"
        edges.append(TypedRelation(
            relationship_type=RelationshipType.TRANSPOSES,
            raw_citation_string=raw,
            dst_id=dst,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING,
        ))
    return edges


# -- pure Formex helpers ----------------------------------------------------
def unzip_formex(raw: bytes) -> bytes | None:
    """CELLAR returns Formex as a zip; unpack the first XML member (pure). Returns
    the raw XML bytes, or None if the payload isn't a usable Formex archive."""
    if raw[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith((".xml", ".fmx", ".fmx4"))]
                if not names:
                    names = zf.namelist()
                if names:
                    return zf.read(names[0])
        except zipfile.BadZipFile:
            return None
        return None
    if raw[:5] == b"<?xml" or b"<?xml" in raw[:100]:
        return raw  # already raw XML
    return None


_P_RE = re.compile(r"<p>(.*?)</p>", re.DOTALL | re.IGNORECASE)
# EUR-Lex national-measure codes like *A9* (order of reference) prefixing a line.
_CODE_RE = re.compile(r"^\*[A-Z0-9]+\*\s*")


@dataclass(frozen=True, slots=True)
class NationalRef:
    """A national referring judgment behind a CJEU preliminary ruling. Recorded now
    even though the national case isn't in the corpus; scraped/harvested later when
    a national adapter exists (the dangling-edge → worklist pattern, §5b)."""

    court: str
    reference: str  # full court + order/case text
    url: str | None  # a scrape target when the source gives one


def parse_national_judgements(blobs: list[str]) -> list[NationalRef]:
    """Parse CELLAR `case-law_national-judgement` blobs (pure). Each blob is a small
    HTML fragment of `<p>` lines: the referring court/case line, sometimes a URL,
    sometimes a publication note."""
    refs: list[NationalRef] = []
    for blob in blobs:
        if not blob:
            continue
        lines = [ln.strip() for ln in _P_RE.findall(blob)] or [blob.strip()]
        court_line = url = None
        for ln in lines:
            if ln.lower().startswith(("http://", "https://")):
                url = url or ln
            elif court_line is None and ln:
                court_line = _CODE_RE.sub("", ln).strip()
        if court_line:
            court = court_line.split(",", 1)[0].strip()
            refs.append(NationalRef(court=court, reference=court_line, url=url))
    return refs


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_text(elem: ET.Element) -> Iterator[str]:
    if elem.text and elem.text.strip():
        yield elem.text.strip()
    for child in elem:
        yield from _iter_text(child)
        if child.tail and child.tail.strip():
            yield child.tail.strip()


def extract_formex(xml_bytes: bytes) -> tuple[str | None, list[Segment]]:
    """Text + structural segments from a Formex judgment (pure, §6b).

    Native units: the reasoning's numbered paragraphs (`<NP.ECR>`, labelled by
    their `<NO.P>` number — the citable unit) followed by the operative ruling
    (`<JURISDICTION>`). Falls back to `<GR.SEQ>` sections, then to whole-document
    text as one block, so a chunkable result always comes back."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None, []

    # The reasoning's numbered paragraphs. Modern Formex uses <NP.ECR>; OLDER judgments
    # (and some manifestations) put the grounds in <GR.SEQ> sections with plain <NP>/<PARAG>
    # instead — finding none and appending only the <JURISDICTION> ruling would leave a
    # ruling-only stub (the dispositif, ~tens of words). So when there are no NP.ECR
    # paragraphs, fall back to the grounds sections to capture the FULL reasoning.
    paras = blocks_by_localname(
        root, {"NP.ECR"}, kind="paragraph", label_child="NO.P", counter_label="para"
    )
    if not paras:
        paras = blocks_by_localname(root, {"GR.SEQ"}, kind="section", counter_label="section")
    blocks = list(paras)
    ruling = next((e for e in root.iter() if _localname(e.tag) == "JURISDICTION"), None)
    if ruling is not None:
        blocks.append(("ruling", "ruling", element_text(ruling)))
    if not blocks:  # nothing structural at all → whole document as one block
        blocks = [("document", "section", element_text(root))]

    text, segments = assemble(blocks)
    return (text or None), segments


def extract_formex_text(xml_bytes: bytes) -> str | None:
    """Flat text only (kept for callers that don't need segments)."""
    return extract_formex(xml_bytes)[0]


_PARTY_NOISE = re.compile(
    r",?\s*(?:represented\b|acting as|applicant|applicants|defendant|defendants|appellant|"
    r"appellants|respondent|respondents|supported by|intervening|the other part|"
    r"established in|residing in|whose registered office)",
    re.IGNORECASE,
)


def _clean_parties(parties: str) -> str:
    """Reduce a Formex <PARTIES> line to the bare case name. For a direct action it's
    laden with representation boilerplate ("X, represented by …, acting as Agent, …,
    applicant, v Y, …, defendant, supported by …") — keep just "X v Y"."""
    def core(side: str) -> str:
        return _PARTY_NOISE.split(side, maxsplit=1)[0].strip().strip(",").strip()

    halves = re.split(r"\s+v\.?\s+", parties, maxsplit=1)
    if len(halves) == 2:
        a, b = core(halves[0]), core(halves[1])
        if a and b:
            return f"{a} v {b}"
    return core(parties)


def formex_case_title(xml_bytes: bytes) -> str | None:
    """A concise case name from a CJEU Formex judgment — the ``<PARTIES>`` line + the
    ``<NO.CASE>`` number, e.g. "ZZ v Secretary of State for the Home Department (C-300/11)".
    Used when the CELLAR webservice gave no title (≈half of them)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    parties = no_case = header_title = None
    for e in root.iter():
        ln = _localname(e.tag)
        if ln == "PARTIES" and parties is None:
            parties = " ".join(t.strip() for t in e.itertext() if t.strip())
        elif ln == "NO.CASE" and no_case is None:
            no_case = " ".join(e.itertext())
        elif ln == "TITLE" and header_title is None:
            header_title = " ".join(t.strip() for t in e.itertext() if t.strip())
    # Modern AG Formex has no <PARTIES>.  Its first TITLE nevertheless contains
    # ``Case C-340/21 VB v Natsionalna… (Request for …)``.  Recover that caption;
    # previously these opinions fell back to a blank title or, worse, their ECLI.
    if not parties and header_title:
        header_title = re.sub(r"\s+", " ", header_title)
        m = re.search(r"\bCase\s+[CTF]?[-‑–]?\d+/\d+\s*(.+?)(?:\(Request\b|$)",
                      header_title, re.IGNORECASE)
        if m:
            parties = m.group(1).strip(" .,—-")
            # Formex inline nodes concatenate around the party separator.
            parties = re.sub(r"(?<=[A-Za-zÀ-ÿ])v(?=[A-ZÀ-Þ])", " v ", parties)
    if not parties:
        return None
    parties = _clean_parties(re.sub(r"\s+", " ", parties))
    if not parties:
        return None
    if no_case:
        no_case = re.sub(r"\s+", "", no_case).replace("‑", "-").replace("–", "-")
        if re.fullmatch(r"\d+/\d+", no_case):
            no_case = "C-" + no_case
        if no_case:
            return clean_case_display_title(f"{parties} ({no_case})") or parties
    return clean_case_display_title(parties)


class EUCellarAdapter(BaseAdapter):
    source = "eu-cellar"
    # SPARQL/REST endpoint; no published hard limit, but pace politely (§1.8).
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        legislation_celex: str | None = None,
        cited_by_celex: str | None = None,
        per_page: int = 100,
        with_citations: bool = True,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.legislation_celex = legislation_celex
        # when set, discover finds CJEU cases that CITE this case (the inverse of
        # work_cites_work) — i.e. "what later judgments cite this one" (forward-citation
        # discovery for a *case*, distinct from cases *interpreting legislation*).
        self.cited_by_celex = cited_by_celex
        self.per_page = per_page
        self.with_citations = with_citations
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    # -- SPARQL ------------------------------------------------------------
    def _sparql(self, query: str) -> list[dict]:
        resp = self._client.request(
            "POST",
            SPARQL_ENDPOINT,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        bindings = resp.json().get("results", {}).get("bindings", [])
        return [{k: v["value"] for k, v in row.items()} for row in bindings]

    def _discover_query(self, since: str | None) -> str:
        link_values = " ".join(f"cdm:{p}" for p in _LEGISLATION_LINKS)
        date_filter = f'FILTER(STR(?date) >= "{since}")' if since else ""
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?celex ?ecli ?date ?link ?rtype ?title WHERE {{
  VALUES ?linkProp {{ {link_values} }}
  ?work cdm:resource_legal_id_celex ?celex .
  # Court of Justice / General Court / Civil Service Tribunal, all instruments
  # (judgments J, orders O, Opinions of the Court V, AG opinions C/A).
  FILTER(REGEX(STR(?celex), "^6[0-9]{{4}}[CTF][JOVCA]"))
  ?work ?linkProp ?legWork .
  ?legWork cdm:resource_legal_id_celex ?leg .
  FILTER(STR(?leg) = "{self.legislation_celex}")
  OPTIONAL {{ ?work cdm:case-law_ecli ?ecli }}
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
  OPTIONAL {{ ?work cdm:work_has_resource-type ?rt .
             BIND(REPLACE(STR(?rt), "^.*resource-type/", "") AS ?rtype) }}
  OPTIONAL {{ ?work cdm:work_has_expression ?exp .
             ?exp cdm:expression_uses_language ?lg . FILTER(STRENDS(STR(?lg), "/ENG"))
             ?exp cdm:expression_title ?title }}
  {date_filter}
  BIND(REPLACE(STR(?linkProp), "^.*#", "") AS ?link)
}}
ORDER BY DESC(?date)
LIMIT {self.per_page}
"""

    def _national_query(self, celex: str) -> str:
        """The referring national court/case (preliminary references) + country."""
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?njudg ?country WHERE {{
  ?w cdm:resource_legal_id_celex ?wc . FILTER(STR(?wc) = "{celex}")
  OPTIONAL {{ ?w cdm:case-law_national-judgement ?njudg }}
  OPTIONAL {{ ?w cdm:case-law_originates_in_country ?cu .
             BIND(REPLACE(STR(?cu), "^.*country/", "") AS ?country) }}
}}
LIMIT 50
"""

    def _cited_query(self, celex: str) -> str:
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?cited_celex ?cited_ecli WHERE {{
  ?w cdm:resource_legal_id_celex ?wc . FILTER(STR(?wc) = "{celex}")
  ?w cdm:work_cites_work ?cw .
  ?cw cdm:resource_legal_id_celex ?cited_celex .
  OPTIONAL {{ ?cw cdm:case-law_ecli ?cited_ecli }}
}}
LIMIT 200
"""

    def _citing_query(self, celex: str) -> str:
        """CJEU cases that CITE the target case (inverse work_cites_work) — the
        forward-citation discovery for a judgment."""
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?celex ?ecli ?date ?rtype ?title WHERE {{
  ?target cdm:resource_legal_id_celex ?tc . FILTER(STR(?tc) = "{celex}")
  ?work cdm:work_cites_work ?target .
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER(REGEX(STR(?celex), "^6[0-9]{{4}}[CTF][JOVCA]"))
  OPTIONAL {{ ?work cdm:case-law_ecli ?ecli }}
  OPTIONAL {{ ?work cdm:work_date_document ?date }}
  OPTIONAL {{ ?work cdm:work_has_resource-type ?rt .
             BIND(REPLACE(STR(?rt), "^.*resource-type/", "") AS ?rtype) }}
  OPTIONAL {{ ?work cdm:work_has_expression ?exp .
             ?exp cdm:expression_uses_language ?lg . FILTER(STRENDS(STR(?lg), "/ENG"))
             ?exp cdm:expression_title ?title }}
}}
ORDER BY DESC(?date)
LIMIT {self.per_page}
"""

    def citing_works(self, celex: str) -> list[dict]:
        """The CJEU cases that CITE ``celex`` — just their ids (celex/ecli/date), NOT their
        full text. One SPARQL call; used by the deferred expand-citing sweep to record
        backward-citation edges fast and pull the bodies later. Thread-safe."""
        return self._sparql(self._citing_query(celex))

    # -- adapter contract --------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        # Either cases CITING a target case (cited_by_celex) or cases linked to a piece
        # of legislation. One of the two must be set — this adapter discovers case law
        # *relative to a named instrument or case*, so with neither there is nothing to
        # crawl. (max_pages reserved for OFFSET paging in a later pass.)
        if not self.cited_by_celex and not self.legislation_celex:
            return
        query = self._citing_query(self.cited_by_celex) if self.cited_by_celex else self._discover_query(since)
        for row in self._sparql(query):
            celex = row["celex"]
            ecli = row.get("ecli")
            _doc_type, court = classify_celex(celex, row.get("rtype"))
            yield Stub(
                stable_id=ecli or celex,  # ECLI is the primary key where present (§1.1)
                landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                raw_url=f"{CELEX_BASE}/{celex}",
                hint_date=_parse_iso(row.get("date")),
                # ECLI/CELEX identify the document; they are not case names.  Keeping
                # either in ``title`` prevents fetch() from deriving the parties from
                # Formex and later makes OSCOLA italicise the identifier as a name.
                title=row.get("title"),
                court=court,
                hints={"celex": celex, "link": row.get("link", ""), "rtype": row.get("rtype", "")},
            )

    def fetch(self, stub: Stub) -> Record | None:
        celex = stub.hints.get("celex") or stub.stable_id
        doc_type, court = classify_celex(celex, stub.hints.get("rtype"))
        raw = self._fetch_formex(stub.raw_url)
        if raw is not None:
            text, segments = extract_formex(raw)
            raw_ext = "xml"
        else:
            # Formex not available (common for pre-2010 cases) — fall back to HTML.
            html = self._fetch_html(stub.raw_url)
            if html is not None:
                text = self._html_to_text(html)
                segments = []
                raw, raw_ext = html, "html"
            else:
                text, segments, raw_ext = None, [], "txt"
        # the CELLAR webservice often gives no title — derive a concise case name from
        # the judgment's own parties + case number ("ZZ v … (C-300/11)").
        formex_title = formex_case_title(raw) if raw is not None and raw_ext == "xml" else None
        generic = bool(stub.title and (
            re.fullmatch(r"(?i)ECLI:[A-Z]{2}:.+", stub.title.strip())
            or stub.title.strip() == celex
            or re.fullmatch(r"(?i)(?:Joined\s+)?Cases?\s+[CTF][-‑–]?\d+/\d+", stub.title.strip())
        ))
        title = formex_title or (None if generic else stub.title)

        relations: list[TypedRelation] = []
        # 1) the typed edge to the legislation that surfaced this case (§1A).
        link_prop = stub.hints.get("link", "")
        rel_type = _LEGISLATION_LINKS.get(link_prop, RelationshipType.MENTIONS)
        relations.append(
            TypedRelation(
                relationship_type=rel_type,
                raw_citation_string=f"{link_prop} {self.legislation_celex}".strip(),
                dst_id=self.legislation_celex,  # legislation keyed by CELEX
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            )
        )
        # 2) mentions edges to the cases this case cites (the CELLAR citation graph).
        if self.with_citations:
            for cited in self._sparql(self._cited_query(celex)):
                dst = cited.get("cited_ecli") or cited.get("cited_celex")
                relations.append(
                    TypedRelation(
                        relationship_type=RelationshipType.MENTIONS,
                        raw_citation_string=cited.get("cited_celex"),
                        dst_id=dst,
                        extracted_via=ExtractedVia.STRUCTURED,
                        resolution_status=ResolutionStatus.PENDING,
                    )
                )

        # 2b) AG Opinion → its judgment: the AG opinion and the judgment share the
        # case number, differing only in the CELEX descriptor (CC/CA vs CJ). Link
        # them (resolves to the judgment's ECLI via the CELEX→ECLI alias, §5b).
        if doc_type == DocType.OPINION and re.match(r"^6\d{4}C[CA]\d{4}$", celex):
            judgment_celex = celex[:5] + "CJ" + celex[7:]
            relations.append(
                TypedRelation(
                    relationship_type=RelationshipType.OPINION_IN,
                    raw_citation_string=judgment_celex,
                    dst_id=judgment_celex,
                    extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.PENDING,
                )
            )

        # 3) preliminary-reference edges to the referring national court/case.
        # The national case isn't in CELLAR — record it as a dangling edge now
        # (dst unresolved), preserving any scrape URL, so it surfaces in the §8
        # harvest worklist and resolves when a national adapter harvests it later.
        nat_rows = self._sparql(self._national_query(celex))
        origin_country = next((r["country"] for r in nat_rows if r.get("country")), None)
        referring_courts: list[str] = []
        for nref in parse_national_judgements([r["njudg"] for r in nat_rows if r.get("njudg")]):
            referring_courts.append(nref.court)
            # embed the origin country so the §5 extractor can tell a UK referral from a
            # foreign one (it gates UK-statute resolution inside CJEU judgments on this).
            country_tag = f" | country: {origin_country}" if origin_country else ""
            ref_string = nref.reference + country_tag + (f" | {nref.url}" if nref.url else "")
            relations.append(
                TypedRelation(
                    relationship_type=RelationshipType.PRELIMINARY_REFERENCE,
                    raw_citation_string=ref_string,  # carries the scrape target for later
                    dst_id=None,  # national case not in corpus yet → worklist (§5b)
                    extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.PENDING,
                )
            )

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            ecli=stub.stable_id if stub.stable_id.startswith("ECLI:") else None,
            doc_type=doc_type,
            title=title,
            court=court,
            decision_date=stub.hint_date,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw if raw is not None else stub.raw_url.encode(),
            raw_ext=raw_ext,
            text=text,
            segments=segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={
                "celex": celex,
                **("html_fallback" and {"content_format": "html"} if raw_ext == "html" else {}),
                **({"origin_country": origin_country} if origin_country else {}),
                **({"referring_courts": referring_courts} if referring_courts else {}),
            },
        )

    def _fetch_formex(self, url: str) -> bytes | None:
        """Best-effort Formex fetch: a 404/406 (no Formex rendition) is not fatal —
        the case is still catalogued with its SPARQL metadata + edges."""
        try:
            resp = self._client.get(
                url,
                headers={"Accept": "application/zip;mtype=fmx4", "Accept-Language": "eng"},
            )
        except FetchError:
            return None
        return unzip_formex(resp.content)

    def _fetch_html(self, url: str) -> bytes | None:
        """HTML fallback: fetch the EUR-Lex HTML rendering when no Formex exists.
        Many pre-2010 CJEU cases have no Formex in CELLAR but do have HTML.
        The same CELLAR content-negotiation URL serves HTML with the right Accept header."""
        try:
            resp = self._client.get(
                url,
                headers={"Accept": "text/html;q=0.9,*/*;q=0.8", "Accept-Language": "en"},
            )
        except FetchError:
            return None
        content = resp.content
        low = content[:512].lower()
        if b"<html" in low or b"<!doctype" in low:
            return content
        return None

    @staticmethod
    def _html_to_text(html_bytes: bytes) -> str | None:
        """Strip EUR-Lex HTML to judgment text (best-effort). Targets the known content
        div to cut navigation noise; falls back to full page text if not found."""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_bytes, "html.parser")
            for junk in soup(["script", "style", "nav", "header", "footer"]):
                junk.decompose()
            # EUR-Lex judgment HTML puts the text in one of these containers:
            body = (soup.find(id="document-content")
                    or soup.find(class_="EurlexContent")
                    or soup.find(id="mainContent")
                    or soup.body)
            if body is None:
                return None
            import re as _re
            text = body.get_text("\n", strip=True)
            return _re.sub(r"\n{3,}", "\n\n", text).strip() or None
        except Exception:  # noqa: BLE001 — best-effort
            return None

    def case_metadata(self, *, celex: str | None = None, ecli: str | None = None) -> dict:
        """One SPARQL hop returning ``{celex, ecli, title}`` for a CJEU case, keyed by
        either its CELEX or its ECLI — so a single case fetched by case-number is keyed
        by ECLI like the rest (CELEX→ECLI alias minted), an ECLI candidate maps to its
        CELEX for the REST fetch, and the **case name** comes along too. Best-effort."""
        if celex:
            bind = f'?w cdm:resource_legal_id_celex ?c . FILTER(STR(?c) = "{celex}")'
        elif ecli:
            # FILTER(STR(...)) matches regardless of the literal's datatype/lang.
            bind = (f'?w cdm:case-law_ecli ?el . FILTER(STR(?el) = "{ecli}") '
                    "?w cdm:resource_legal_id_celex ?c .")
        else:
            return {}
        # title via expression (best-effort: free CELLAR RDF often omits the party
        # name — the authoritative title lives in the credentialed EUR-Lex webservice).
        q = (
            f"PREFIX cdm: <{CDM}>\n"
            "SELECT ?c ?ecli ?title WHERE { " + bind +
            " OPTIONAL { ?w cdm:case-law_ecli ?ecli }"
            " OPTIONAL { ?w cdm:expression_title ?title } } LIMIT 1"
        )
        try:
            rows = self._sparql(q)
        except Exception:  # noqa: BLE001 — best-effort; callers tolerate {}
            return {}
        if not rows:
            return {}
        r = rows[0]
        return {"celex": r.get("c") or celex, "ecli": r.get("ecli") or ecli, "title": r.get("title")}

    def celex_for_eclis(self, eclis: list[str]) -> dict[str, str]:
        """Batched ECLI → CELEX in **one** SPARQL (CELLAR SPARQL is free/unmetered),
        so the credentialed webservice title lookup can then be batched by CELEX."""
        eclis = [e for e in dict.fromkeys(eclis) if e]
        out: dict[str, str] = {}
        for i in range(0, len(eclis), 25):  # chunk — a huge VALUES join can time out
            values = " ".join(f'"{e}"' for e in eclis[i: i + 25])
            q = (
                f"PREFIX cdm: <{CDM}>\n"
                "SELECT ?e ?celex WHERE { "
                f"VALUES ?e {{ {values} }} "
                "?w cdm:case-law_ecli ?el . FILTER(STR(?el) = ?e) "
                "?w cdm:resource_legal_id_celex ?celex . } "
            )
            try:
                rows = self._sparql(q)
            except Exception:  # noqa: BLE001
                continue
            for r in rows:
                if r.get("e") and r.get("celex"):
                    out[r["e"]] = r["celex"]
        return out


# A case number ("C-217/12") says nothing about how the case ENDED, nor reliably which
# court heard it, but a CELEX must encode both: the descriptor is court (C/T/F) + type
# (J judgment, O order, C Opinion of the AG, V Opinion of the Court). The grammar can only
# guess — it guesses a CJ judgment — so a case that ended in an order, was an AG opinion,
# or was actually heard by the General Court is minted as a CELEX that does not exist. So
# instead of probing a couple of hand-picked variants, ask CELLAR which descriptor the
# number REALLY has, and rank the answers: prefer the citation's own court family, and
# within a family a decision (judgment > order > opinion) over an ancillary notice.
_DECISION_DESCRIPTORS = {
    "C": ("CJ", "CO", "CC", "CV"),   # Court of Justice: judgment, order, AG opinion, Court opinion
    "T": ("TJ", "TO"),                # General Court: judgment, order
    "F": ("FJ", "FO"),                # Civil Service Tribunal (historic)
}
# every descriptor that denotes an actual decision (not a notice/communication), so a
# stray ``…CN…`` / ``…TA…`` OJ notice is never mistaken for the case itself.
_ALL_DECISION_DESCRIPTORS = frozenset(d for ds in _DECISION_DESCRIPTORS.values() for d in ds)
# A case CELEX: 5-digit sector+year, a 1- or 2-letter descriptor, a 4-digit case number.
# The descriptor length varies (legacy "61994J0334" vs modern "62016CJ0113"), so it must
# be matched rather than sliced at a fixed offset.
_CASE_CELEX_RE = re.compile(r"^(?P<year>\d{5})(?P<desc>[A-Z]{1,2})(?P<num>\d{4})$")
# The legacy single-letter decision types, mapped to how they rank against the modern
# two-letter descriptors: a bare "J" is a judgment, "O" an order, "A"/"C" an opinion.
_LEGACY_DESCRIPTOR_TYPE = {"J": "J", "O": "O", "A": "C", "C": "C", "V": "V"}


def _ranked_descriptors(family: str, guessed_desc: str) -> list[str]:
    """Which CELEX descriptors to accept, best first, for a guessed case descriptor.

    With a court family known ("CJ" → C), prefer that family's decisions (judgment >
    order > opinion) and fall back to the others — a "C-" citation that only exists as
    a "T-" case is a citation error we still want to resolve. With only a legacy type
    letter ("J"), the family is unknown, so prefer that TYPE across every family: a
    cited *order* should resolve to the order rather than to the judgment in the same
    case."""
    every = [d for ds in _DECISION_DESCRIPTORS.values() for d in ds]
    if family:
        ranked = list(_DECISION_DESCRIPTORS.get(family, ()))
        return ranked + [d for d in every if d not in ranked]
    want = _LEGACY_DESCRIPTOR_TYPE.get(guessed_desc, "")
    ranked = [d for d in every if want and d[1] == want]
    return ranked + [d for d in every if d not in ranked]


class CellarUnavailable(Exception):
    """A CELLAR SPARQL lookup failed to complete (timeout, 5xx, rate-limit exhaustion).

    Distinct from an *empty* result: an empty result means the case is genuinely not in
    CELLAR (a real absence → 90-day cooldown), but a failed lookup tells us **nothing**
    about the case's existence. Collapsing the two — returning ``None`` on both — is what
    let a flaky CELLAR moment brand tens of thousands of held CJEU cases "absent" for 90
    days, so the drain never retried them. This exception propagates out of the targeted
    builder, and :meth:`Facade._fetch_reference` classifies it as *transient* (retry in
    hours), not absent."""


def resolve_case_celex(celex: str, *, client: RateLimitedClient | None = None) -> str | None:
    """The CELEX that actually exists in CELLAR for a guessed case CELEX, or None if the
    case is genuinely absent (§5b). Raises :class:`CellarUnavailable` if the lookup can't
    be completed (so the caller retries later rather than writing the case off).

    One SPARQL hop finds every descriptor CELLAR holds for the case *number*
    (``62016CJ0113`` guessed → CELLAR has ``62016CC0113`` + ``62016TJ0113``); we then pick
    the best decision, preferring the citation's court family (a ``C-`` cite → a C-sector
    descriptor) and a judgment over an order over an opinion. The caller aliases the guess
    to the resolved document, so this lookup is paid once per cited case, not per citation."""
    cu = (celex or "").upper()
    m = _CASE_CELEX_RE.match(cu)
    if m is None:
        return None
    year, guessed_desc, num = m.group("year"), m.group("desc"), m.group("num")
    # The descriptor is 1 OR 2 letters. Modern CELEX writes both the court family and
    # the decision type ("CJ" = Court of Justice judgment); the LEGACY form writes only
    # the type ("61994J0334"). Slicing a fixed two characters mis-split the legacy form —
    # it read the descriptor as "J0" and the case number as "334", losing the leading
    # zero, so the lookup regex could never match and *every* legacy-form citation was
    # written off as absent. 61994J0334 is really 61994CJ0334, and CELLAR has it.
    family = guessed_desc[0] if len(guessed_desc) == 2 else ""
    cellar = EUCellarAdapter(client=client)
    q = (
        f"PREFIX cdm: <{CDM}>\n"
        "SELECT DISTINCT ?celex WHERE { ?w cdm:resource_legal_id_celex ?celex . "
        f'FILTER(REGEX(STR(?celex), "^{year}[A-Z][A-Z]{num}$")) }}'
    )
    try:
        found = {r["celex"].upper() for r in cellar._sparql(q) if r.get("celex")}
    except Exception as exc:  # noqa: BLE001 — transport/CELLAR failure, NOT an absence
        raise CellarUnavailable(f"CELLAR lookup failed for {cu}: {exc}") from exc
    # ranked preference: this family's decisions (best type first), then the other
    # families' decisions (a "C-" citation that only exists as a "T-" case = a citation
    # error we still want to resolve).
    ranked = _ranked_descriptors(family, guessed_desc)
    for desc in ranked:
        cand = f"{year}{desc}{num}"
        if cand in found:
            return cand
    # a decision descriptor we don't rank explicitly, but still a real decision
    for cand in sorted(found):
        if cand[5:7] in _ALL_DECISION_DESCRIPTORS:
            return cand
    # Joined cases: the decision is published only under the LEAD case number
    # (Joined Cases C-46/93 and C-48/93 → 61993CJ0046; no CELEX exists under 0048
    # at all). The lead work links every joined number via
    # cdm:case-law_joins_case_court, whose object URI embeds the joined CELEX
    # (…/resource/case/celex%3A61993CJ0048) — so one reverse hop finds the lead.
    return _resolve_joined_case(cellar, year=year, num=num, family=family)


def _resolve_joined_case(cellar: "EUCellarAdapter", *, year: str, num: str,
                         family: str) -> str | None:
    q = (
        f"PREFIX cdm: <{CDM}>\n"
        "SELECT DISTINCT ?celex WHERE { ?w cdm:case-law_joins_case_court ?j . "
        f'FILTER(REGEX(STR(?j), "celex(%3A|:){year}[A-Z][A-Z]{num}$", "i")) '
        "?w cdm:resource_legal_id_celex ?celex . }"
    )
    try:
        leads = {r["celex"].upper() for r in cellar._sparql(q) if r.get("celex")}
    except Exception as exc:  # noqa: BLE001 — a failed lookup is transient, not an absence
        raise CellarUnavailable(f"CELLAR joined-case lookup failed for {year}/{num}: {exc}") from exc
    leads = {c for c in leads if len(c) >= 9 and c[5:7] in _ALL_DECISION_DESCRIPTORS}
    if not leads:
        return None
    # The lead has a different case NUMBER, so rank by descriptor alone: the cited
    # family's decisions first (judgment > order > opinion), then the rest.
    ranked = _ranked_descriptors(family, "")
    for desc in ranked:
        for cand in sorted(leads):
            if cand[5:7] == desc:
                return cand
    return sorted(leads)[0]


class CJEUCaseAdapter(BaseAdapter):
    """Targeted single-judgment fetch by CELEX (e.g. ``62018CJ0511`` from a citation
    like "C-511/18"). Unlike the legislation-discovery adapter it adds **no** spurious
    interprets-edge — it just fetches that one case's Formex and classifies it,
    keyed by ECLI where resolvable. The clean fetcher behind targeted resolution of
    CJEU case-number citations (§5b)."""

    source = "eu-cellar"
    min_interval = 0.5

    def __init__(self, celex: str, *, client: RateLimitedClient | None = None,
                 celex_aliases: tuple[str, ...] = ()) -> None:
        self.celex = celex.upper()
        # CELEXes the corpus cites this case by but which aren't its real id (a guessed
        # …CJ… for a case that ended in an order). Aliased to the stored document on
        # ingest, so the citing edges resolve.
        self.celex_aliases = tuple(a.upper() for a in celex_aliases if a.upper() != self.celex)
        self._cellar = EUCellarAdapter(client=client)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        yield Stub(stable_id=self.celex, raw_url=f"{CELEX_BASE}/{self.celex}",
                   landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{self.celex}",
                   hints={"celex": self.celex})

    def fetch(self, stub: Stub) -> Record | None:
        celex = self.celex
        doc_type, court = classify_celex(celex)
        meta = self._cellar.case_metadata(celex=celex)
        ecli, title = meta.get("ecli"), meta.get("title")
        raw = self._cellar._fetch_formex(stub.raw_url)
        if raw is not None:
            text, segments = extract_formex(raw)
            raw_bytes, raw_ext = raw, "xml"
        else:
            # Formex unavailable — try HTML (common for pre-2010 cases in CELLAR).
            html = self._cellar._fetch_html(stub.raw_url)
            if html is not None:
                text = self._cellar._html_to_text(html)
                segments = []
                raw_bytes, raw_ext = html, "html"
            else:
                text, segments, raw_bytes, raw_ext = None, [], None, "txt"
        # Return None only when we have literally nothing — no content AND no ECLI
        # to key the record by. If we have any content, store it (metadata-only is
        # already handled by SPARQL in EUCellarAdapter; this path is targeted fetch).
        if raw_bytes is None and ecli is None:
            return None  # genuinely absent from CELLAR — let the caller report "not found"
        return Record(
            source=self.source, stable_id=ecli or celex,
            ecli=ecli, doc_type=doc_type, court=court, title=title,
            landing_url=stub.landing_url,
            raw_bytes=raw_bytes if raw_bytes is not None else celex.encode(),
            raw_ext=raw_ext,
            text=text, segments=segments, extracted_via=ExtractedVia.STRUCTURED,
            extra={"celex": celex,
                   **({"celex_aliases": list(self.celex_aliases)} if self.celex_aliases else {}),
                   **("html_fallback" and {"content_format": "html"} if raw_ext == "html" else {})},
        )
