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

The default discovery path is now a date-filtered, newest-first enumeration of all CJEU
case-law instruments, making CELLAR the live currency layer over the held case corpus.
Set `legislation_celex` to follow the case law on one piece of legislation, or
`cited_by_celex` to find judgments citing a given case. In the targeted legislation mode,
each case yields a typed edge to that legislation (`interprets`/`applies`/`overrules`);
all modes add `mentions` edges to cited cases, with ECLI destinations where available.

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
_DOCKET_PAREN_RE = re.compile(
    r"\s*\((?:(?:Joined\s+)?Cases?\s+)?[CTF][-‑–]?\d+/\d+(?:\s*(?:P|RX))?"
    r"(?:\s*(?:,|and|to|et|&)\s*[CTF][-‑–]?\d+/\d+(?:\s*(?:P|RX))?)*\)",
    re.IGNORECASE,
)


def clean_case_display_title(title: str | None) -> str | None:
    """Drop a terminal parenthesised C/T/F docket echo from a party-name title.
    The ECLI/CELEX already carries identity; ``OC (C-479/22P)`` should display as
    ``OC``. Covers Court, General Court, Civil Service and appeal/RX suffixes."""
    if not title:
        return title
    return re.sub(r"\s{2,}", " ", _DOCKET_PAREN_RE.sub("", title)).strip()


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
#   Canada PNR) · *C = Advocate General conclusions.  *A and *N are OJ
#   information notices (respectively the result and the application), not opinions.
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
    "A": DocType.NOTE,  # OJ notice of the judgment/order (operative part)
    "N": DocType.NOTE,  # OJ notice of the application/reference
}
# CDM work_has_resource-type prefixes → doc_type (authoritative when available).
_RESOURCE_TYPE_DOCTYPE = {
    "JUDG": DocType.JUDGMENT,
    "ORDER": DocType.DECISION,
    "OPIN_JUR": DocType.OPINION,  # Opinion of the Court
    "OPIN_AG": DocType.OPINION,
    "VIEW": DocType.OPINION,
    "INFO_JUDICIAL": DocType.NOTE,
}


def classify_celex(celex: str | None, resource_type: str | None = None) -> tuple[DocType, str]:
    """Map a CJEU CELEX (+ optional CDM resource-type) to (doc_type, court).
    Falls back sensibly so an unrecognised descriptor still catalogues as a CoJ
    judgment rather than crashing."""
    # Sector is load-bearing: 3 = legislation, 0 = a consolidated version of legislation,
    # 1/2 = treaties/international agreements, 5 = preparatory acts, 6 = case law. Only the
    # last is a judgment; classify the legislative sectors as LEGISLATION so a consolidated
    # or base act isn't mis-catalogued as a CoJ judgment.
    _sector = (celex or "").strip()[:1]
    if _sector in ("0", "1", "2", "3", "4"):
        return DocType.LEGISLATION, "European Union"
    court = "Court of Justice"
    doc_type = DocType.JUDGMENT
    m = re.match(r"^6\d{4}([CTF])([A-Z])", celex or "")
    if m:
        court = _COURT_BY_SECTOR.get(m.group(1), court)
        doc_type = _DOCTYPE_BY_DESCRIPTOR.get(m.group(2), DocType.JUDGMENT)
        if m.group(2) == "C":  # AG conclusions
            court = "Advocate General"
    if resource_type:
        rt = resource_type.upper()
        for prefix, dt in _RESOURCE_TYPE_DOCTYPE.items():
            if rt.startswith(prefix):
                doc_type = dt
                break
    return doc_type, court


def _eu_currency_meta(celex: str | None, meta: dict | None = None) -> dict:
    """Unified legislative currency (§CUR) for an EU act: consolidation snapshot + point-in-time
    capability, plus EUR-Lex's in-force descriptor when the SPARQL metadata carried one."""
    from ..leg_currency import currency_for_eu
    in_force = (meta or {}).get("in_force") or (meta or {}).get("in_force_status")
    return currency_for_eu(celex, in_force=in_force).to_meta()

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
    # CELLAR models these resources as ``measure_national_implementing`` works.
    # The older/general-looking ``resource_legal_implements_resource_legal``
    # predicate returns no rows in the current CDM.
    nim_prefix = "7" + celex[1:]
    return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?nim ?nimCelex ?country ?eli ?title WHERE {{
  ?dir cdm:resource_legal_id_celex ?dc . FILTER(STR(?dc) = "{celex}")
  ?nim cdm:measure_national_implementing_implements_resource_legal ?dir .
  ?nim cdm:resource_legal_id_celex ?nimCelex .
  FILTER(STRSTARTS(STR(?nimCelex), "{nim_prefix}"))
  OPTIONAL {{ ?nim cdm:measure_national_implementing_implemented_by_country ?c .
             BIND(REPLACE(STR(?c), "^.*/", "") AS ?country) }}
  OPTIONAL {{ ?nim cdm:resource_legal_id_local ?eli }}
  OPTIONAL {{ ?nim cdm:work_title ?title }}
}}
LIMIT 500
"""


def national_transposition_edges(celex: str, sparql) -> list[TypedRelation]:
    """`transposes` edges from a directive CELEX to its national implementing measures,
    using a caller-supplied ``sparql(query) -> list[dict]``. Most destinations are not
    in the corpus yet, so their sector-7 CELEX remains a pending destination and the
    national title/country/local id stays auditable in ``raw_citation_string``. A real
    national ELI, when present, is preferred and can resolve directly against a domestic
    corpus."""
    edges: list[TypedRelation] = []
    seen: set[str] = set()
    for row in sparql(_transposition_query(celex)):
        eli = row.get("eli") or ""
        m = _NIM_ELI_RE.search(eli)
        dst = m.group(1).rstrip("/") if m else None
        nim_celex = row.get("nimCelex") or ""
        title = row.get("title") or nim_celex or eli or row.get("nim")
        country = row.get("country")
        # Sector-7 CELEX is the only universal identifier.  Keep it as the
        # pending destination when CELLAR does not expose a national ELI so the
        # measure remains enumerable and can later be aliased to a domestic
        # corpus identifier.
        dst = dst or nim_celex or None
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


# Formex block elements that deserve their own laid-out line(s); numbering/title elements
# whose text is carried in the label or a line prefix instead of duplicated into the body.
_FMX_BLOCK = {"PARAG", "ALINEA", "NP", "P", "ITEM", "SUBPARAG", "POINT", "LIST", "DLIST",
              "DLIST.ITEM", "TBL", "TXT"}
_FMX_SKIP = {"NO.P", "NO.PARAG", "TI.ART", "STI.ART", "TI", "STI"}


def _fmx_render(elem, indent: int = 0) -> list[str]:
    """Lay a Formex block element out as indented lines: each sub-block (`ALINEA`, `NP`,
    list `ITEM`…) on its own line, nested lists indented, and a leading number (`NO.P` /
    `NO.PARAG`) prefixed — so paragraphs and points read as laid out instead of bunching
    into one run, and a pincite to a point resolves to *its* line, not the article end."""
    pad = "    " * indent
    no = next((c for c in elem if _localname(c.tag) in ("NO.P", "NO.PARAG")), None)
    prefix = (element_text(no).strip() + " ") if no is not None else ""
    inline: list[str] = []
    child_lines: list[str] = []
    if elem.text and elem.text.strip():
        inline.append(elem.text.strip())
    for c in elem:
        cl = _localname(c.tag)
        if cl in _FMX_SKIP:
            pass  # its text is the label / line prefix, not body
        elif cl in _FMX_BLOCK:
            child_lines.extend(_fmx_render(c, indent + 1))
        else:
            t = element_text(c).strip()
            if t:
                inline.append(t)
        if c.tail and c.tail.strip():
            inline.append(c.tail.strip())
    lines: list[str] = []
    head = (prefix + " ".join(inline)).strip()
    if head:
        lines.append(pad + head)
    lines.extend(child_lines)
    return lines


def _fmx_first_child_text(elem, name: str) -> str | None:
    c = next((x for x in elem.iter() if _localname(x.tag) == name), None)
    return element_text(c).strip() if c is not None else None


def _article_number(ti_art: str | None, fallback: int) -> str:
    m = re.search(r"[Aa]rticle\s+([0-9]+[a-z]?)", ti_art or "")
    return m.group(1) if m else str(fallback)


def _formex_legislation_blocks(root) -> list[tuple[str, str, str]]:
    """(label, kind, text) blocks for an EU legislative act in Formex 4 (the L-series OJ
    structure). Native units, per the Formex manual: the PREAMBLE's ``<VISA>`` legal-basis
    citations and ``<CONSID>`` recitals, then the ENACTING.TERMS ``<ARTICLE>``s — split into
    their numbered ``<PARAG>``s so a pincite to "Article 5(1)" lands on that paragraph, laid
    out with indentation for sub-points — then ``<ANNEX>``es. VISA segments carry the kind
    ``visa`` so a legal-basis pass can key off the structure rather than guessing from text
    position (the basis is not reliably the first citation — procedural visas intersperse it)."""
    blocks: list[tuple[str, str, str]] = []
    seen: set[int] = set()  # de-dupe nested repeats (a CONSID inside GR.CONSID etc.)

    # PREAMBLE — legal bases (VISA) then recitals (CONSID). Order preserved for readability.
    for i, visa in enumerate((e for e in root.iter() if _localname(e.tag) == "VISA"), 1):
        t = "\n".join(_fmx_render(visa)).strip()
        if t and id(visa) not in seen:
            seen.add(id(visa))
            blocks.append((f"Legal basis {i}", "visa", t))
    for i, cons in enumerate((e for e in root.iter() if _localname(e.tag) == "CONSID"), 1):
        if id(cons) in seen:
            continue
        seen.add(id(cons))
        t = "\n".join(_fmx_render(cons)).strip()
        if t:
            no = (_fmx_first_child_text(cons, "NO.P") or "").strip("().[] ")
            blocks.append((f"Recital {no or i}", "recital", t))
    # ENACTING.TERMS — per ARTICLE, and within it per numbered PARAG (Article 5(1), 5(2)…),
    # so pincites resolve to the paragraph, not the whole article.
    for i, art in enumerate((e for e in root.iter() if _localname(e.tag) == "ARTICLE"), 1):
        if id(art) in seen:
            continue
        seen.add(id(art))
        ti = _fmx_first_child_text(art, "TI.ART")
        sti = _fmx_first_child_text(art, "STI.ART")
        art_label = ti or f"Article {i}"
        art_no = _article_number(ti, i)
        parags = [c for c in art if _localname(c.tag) == "PARAG"]
        if parags:
            # a short heading segment so a whole-article pincite ("Article 5") still resolves
            heading = art_label + (f" — {sti}" if sti else "")
            blocks.append((art_label, "article", heading))
            for k, p in enumerate(parags, 1):
                pno = (_fmx_first_child_text(p, "NO.PARAG") or str(k)).strip("().[] ")
                body = "\n".join(_fmx_render(p)).strip()
                if body:
                    blocks.append((f"Article {art_no}({pno})", "paragraph", body))
        else:
            body = "\n".join(_fmx_render(art)).strip()
            if body:
                blocks.append((art_label, "article", body))
    # ANNEXes.
    for i, anx in enumerate((e for e in root.iter() if _localname(e.tag) == "ANNEX"), 1):
        if id(anx) in seen:
            continue
        seen.add(id(anx))
        t = "\n".join(_fmx_render(anx)).strip()
        if t:
            label = _fmx_first_child_text(anx, "TITLE") or _fmx_first_child_text(anx, "TI") or f"Annex {i}"
            blocks.append((label[:60], "annex", t))
    return blocks


def _formex_judgment_blocks(root) -> list[tuple[str, str, str, int]]:
    """``(label, kind, text, level)`` blocks for a CJEU judgment, in reading order.

    A judgment's reasoning is not a flat run of numbered paragraphs: Formex groups it into
    nested ``<GR.SEQ>`` sections, each opening with a ``<TITLE>`` — "Legal context" ›
    "European Union law" › "The GDPR", then "The dispute in the main proceedings…",
    "Consideration of the questions referred", and the question-by-question headings that
    tell a reader what each block of paragraphs is answering. We took only the ``<NP.ECR>``
    paragraphs, so every one of those headings was dropped: the judgment read as an
    unbroken wall of numbered text, where EUR-Lex shows its structure.

    Headings are ONE element type — ``TITLE`` (with its ``TI``, plus ``STI`` where a
    section has a subtitle). The LEVEL is not a different tag but the ``GR.SEQ`` nesting
    depth, which is what the outline uses. The document's own title block sits outside
    ``CONTENTS.JUDGMENT`` and is not a section heading, so the walk starts inside it.
    """
    contents = next((e for e in root.iter()
                     if _localname(e.tag) == "CONTENTS.JUDGMENT"), None)
    if contents is None:
        return []
    blocks: list[tuple[str, str, str, int]] = []
    para_n = 0

    def walk(node, depth: int) -> None:
        nonlocal para_n
        for child in node:
            name = _localname(child.tag)
            if name == "TITLE":
                ti = " ".join(element_text(child).split())
                if ti:
                    # depth 1 for a top-level section, deeper for its subsections
                    blocks.append((ti[:80], "heading", ti, max(1, depth)))
            elif name == "GR.SEQ":
                walk(child, depth + 1)
            elif name == "JURISDICTION":
                continue        # the operative ruling is appended by the caller, once
            elif name == "NP.ECR":
                text = element_text(child)
                if not text.strip():
                    continue
                para_n += 1
                no = next((c for c in child.iter() if _localname(c.tag) == "NO.P"), None)
                label = (no.text or "").strip() if no is not None and no.text else ""
                blocks.append((label or f"para {para_n}", "paragraph", text, 0))
            else:
                text = element_text(child)
                if text.strip():
                    para_n += 1
                    blocks.append((f"para {para_n}", "paragraph", text, 0))

    walk(contents, 0)
    return blocks


def extract_formex(xml_bytes: bytes) -> tuple[str | None, list[Segment]]:
    """Text + structural segments from a Formex 4 instance (pure, §6b).

    Dispatches on the document's own structure: an act with ``<ENACTING.TERMS>`` is
    legislation → article / recital / legal-basis (VISA) segments (``_formex_legislation_
    blocks``); otherwise it is a judgment → the reasoning's numbered paragraphs
    (``<NP.ECR>``, labelled by ``<NO.P>``) plus the operative ``<JURISDICTION>`` ruling,
    falling back to ``<GR.SEQ>`` grounds. Non-Formex EU law never reaches here — it keeps
    its normal (grammar/HTML/AKN) path. A whole-document block is the last resort so a
    chunkable result always comes back."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None, []

    # Legislation: reliable article/recital/legal-basis structure from the act itself.
    if any(_localname(e.tag) == "ENACTING.TERMS" for e in root.iter()):
        blocks = _formex_legislation_blocks(root)
        if blocks:
            text, segments = assemble(blocks)
            if text:
                return text, segments
        # structural parse yielded nothing usable → fall through to whole-document text

    # Judgment: walk CONTENTS.JUDGMENT in reading order so the section HEADINGS survive
    # alongside the numbered paragraphs (see _formex_judgment_blocks). Older judgments have
    # no CONTENTS.JUDGMENT wrapper: fall back to the flat <NP.ECR> scan, then to the
    # <GR.SEQ> grounds sections — finding neither and appending only the <JURISDICTION>
    # ruling would leave a ruling-only stub.
    paras: list[tuple] = _formex_judgment_blocks(root)
    if not paras:
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


def harvest_act_relations(celex: str, *, timeout: float = 60.0) -> list[TypedRelation]:
    """Query CELLAR for a legislative act's act-to-act CDM relationships (property names
    verified live — see eu_law.CDM_ACT_TO_ACT_LINKS) and return them as TypedRelations FROM
    this act: outgoing props as-is (repeals / amends / consolidates / corrects / legal_basis),
    incoming props as their reverse (repealed_by / amended_by / corrected_by) so the act
    carries its own currency. Edges are dangling (the other act may be unheld), so they feed
    the worklist and light up the legislative-status banner even before it is harvested."""
    import httpx

    from ..eu_law import CDM_ACT_TO_ACT_LINKS
    props = " ".join(f"cdm:{p}" for p in CDM_ACT_TO_ACT_LINKS)
    q = (f"PREFIX cdm: <{CDM}>\n"
         "SELECT ?p ?other ?dir WHERE {\n"
         f'  ?w cdm:resource_legal_id_celex ?c . FILTER(STR(?c)="{celex}")\n'
         '  { ?w ?p ?o . ?o cdm:resource_legal_id_celex ?other . BIND("out" AS ?dir) }\n'
         '  UNION\n'
         '  { ?s ?p ?w . ?s cdm:resource_legal_id_celex ?other . BIND("in" AS ?dir) }\n'
         f"  VALUES ?p {{ {props} }}\n}} LIMIT 500")
    try:
        resp = httpx.post(SPARQL_ENDPOINT, data={"query": q},
                          headers={"Accept": "application/sparql-results+json"}, timeout=timeout)
        rows = resp.json().get("results", {}).get("bindings", [])
    except Exception:  # noqa: BLE001 — a CELLAR blip must not kill the enrich pass
        return []
    out: list[TypedRelation] = []
    seen: set[tuple[str, str]] = set()
    for b in rows:
        prop = b.get("p", {}).get("value", "").rsplit("#", 1)[-1]
        other = b.get("other", {}).get("value")
        direction = b.get("dir", {}).get("value")
        mapped = CDM_ACT_TO_ACT_LINKS.get(prop)
        if not mapped or not other:
            continue
        rel_name = mapped[0] if direction == "out" else mapped[1]
        if rel_name is None:
            continue
        key = (rel_name, other)
        if key in seen:
            continue
        seen.add(key)
        out.append(TypedRelation(
            relationship_type=RelationshipType[rel_name], raw_citation_string=other,
            dst_id=other, extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING))
    return out


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


# An AG Opinion prints its author on its face — "OPINION OF ADVOCATE GENERAL / EMILIOU /
# delivered on 15 May 2025" — and nowhere else we hold: the SPARQL metadata omits it and
# these documents' titles are empty, so their OSCOLA citation read "…, Opinion of AG" with
# a blank where the name belongs. The name may sit on the same line as the label or on the
# next one, is printed in caps, and can be several words ("CAMPOS SÁNCHEZ-BORDONA").
_AG_HEAD_RE = re.compile(
    r"(?:OPINION|VIEW)\s+OF\s+(?:MR|MRS|MS)?\s*ADVOCATE\s+GENERAL\s*[\n\s]+"
    r"(?P<name>[^\n]{2,60}?)\s*[\n\s]+"
    r"delivered\s+on\s+(?P<date>\d{1,2}\s+[A-Za-zé]+\s+(?:19|20)\d{2})",
    re.IGNORECASE)


def _titlecase_ag(name: str) -> str:
    """"CAMPOS SÁNCHEZ-BORDONA" → "Campos Sánchez-Bordona"; a name already in mixed case is
    left alone (the source is inconsistent, and lowercasing a correct name is worse)."""
    n = " ".join((name or "").split())
    if not n or n != n.upper():
        return n
    return "-".join(w.capitalize() for w in n.split("-")) if "-" in n and " " not in n else \
        " ".join("-".join(p.capitalize() for p in w.split("-")) for w in n.split())


def parse_ag_opinion_head(text: str | None) -> dict:
    """The Advocate General's name + delivery date from an Opinion's own text.

    Returns ``{}`` when the text isn't an AG Opinion (an Opinion of the Court, a judgment,
    an unparsed scan), so the caller can treat a miss as "no data" rather than an error.
    """
    head = (text or "")[:3000]
    m = _AG_HEAD_RE.search(head)
    if not m:
        return {}
    name = _titlecase_ag(m.group("name"))
    # a stray line ("Provisional text", a footnote marker) is not a name
    if not name or len(name) < 2 or any(ch.isdigit() for ch in name):
        return {}
    return {"advocate_general": name, "delivered_on": " ".join(m.group("date").split())}


def _rendition_language(text: str | None) -> str | None:
    """Distinguish the EN and FR CJEU bodies EUR-Lex can return under the same EN URL."""
    sample = f" {(text or '')[:20000].lower()} "
    french = sum(sample.count(marker) for marker in (
        " arrêt de la cour", " par ces motifs", " dans l’affaire", " dans l'affaire",
        " en vertu de", " la commission européenne", " le royaume", " la république",
    ))
    english = sum(sample.count(marker) for marker in (
        " judgment of the court", " on those grounds", " in case c", " in the present case",
        " the european commission", " the republic", " the kingdom", " hereby",
    ))
    if french >= 2 and french > english:
        return "fr"
    if english >= 2 and english > french:
        return "en"
    return None


def _extract_oj_operative_part(text: str | None) -> str | None:
    """Return the ruling, not the parties/history apparatus, from an English OJ notice."""
    if not text:
        return None
    marker = re.search(r"(?im)^Operative part of (?:the )?(?:judgment|order)\s*$", text)
    if not marker:
        return None
    out = text[marker.end():].strip()
    # Footnote/ELI furniture follows the numbered disposition.
    out = re.split(r"(?im)^ELI:\s|^ISSN\s|^Top\s*$|^\(\s*\n?\s*1\s*\n?\s*\)\s*$", out, maxsplit=1)[0]
    return out.strip() or None


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
  # (judgments J, orders O, Opinions of the Court V, AG conclusions C).
  # A/N are derivative OJ information notices, not separate case-law works.
  FILTER(REGEX(STR(?celex), "^6[0-9]{{4}}[CTF][JOVC]"))
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

    def _enumerate_query(self, since: str | None, offset: int) -> str:
        """All CJEU decisions/opinions, newest first, with a server-side date cursor."""
        date_filter = f'FILTER(STR(?date) > "{since[:10]}")' if since else ""
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?celex ?ecli ?date ?rtype ?title WHERE {{
  ?work cdm:resource_legal_id_celex ?celex .
  FILTER(REGEX(STR(?celex), "^6[0-9]{{4}}[CTF][JOVC][0-9]{{4}}$"))
  ?work cdm:work_date_document ?date .
  {date_filter}
  OPTIONAL {{ ?work cdm:case-law_ecli ?ecli }}
  OPTIONAL {{ ?work cdm:work_has_resource-type ?rt .
             BIND(REPLACE(STR(?rt), "^.*resource-type/", "") AS ?rtype) }}
  OPTIONAL {{ ?work cdm:work_has_expression ?exp .
             ?exp cdm:expression_uses_language ?lg . FILTER(STRENDS(STR(?lg), "/ENG"))
             ?exp cdm:expression_title ?title }}
}}
ORDER BY DESC(?date) ?celex
LIMIT {self.per_page} OFFSET {offset}
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

    def _advocate_general_query(self, celexes: list[str]) -> str:
        """The Advocate General who delivered each of these opinions.

        CELLAR models the AG as a real relation — ``cdm:case-law_delivered_by_advocate-
        general`` onto a ``cdm:person`` whose ``cdm:agent_name`` is the surname the Court
        cites ("Emiliou") — so this is structured data, not something to read off the page.
        Batched: one query answers a whole page of opinions.
        """
        # STR()-compared, like every other query here: the stored CELEX is a typed
        # xsd:string literal, so a plain-literal VALUES block matches nothing on Virtuoso.
        listed = '", "'.join(celexes)
        return f"""
PREFIX cdm: <{CDM}>
SELECT DISTINCT ?celex ?name WHERE {{
  ?w cdm:resource_legal_id_celex ?c .
  FILTER(STR(?c) IN ("{listed}"))
  BIND(STR(?c) AS ?celex)
  ?w cdm:case-law_delivered_by_advocate-general ?ag .
  ?ag cdm:agent_name ?name .
}}
LIMIT {max(50, len(celexes) * 2)}
"""

    def advocate_generals(self, celexes: list[str]) -> dict[str, str]:
        """``{celex: AG surname}`` for a batch of opinions — one query for the lot, so a
        backfill over thousands is ~40 round trips rather than thousands."""
        wanted = [c for c in dict.fromkeys(celexes) if c]
        if not wanted:
            return {}
        try:
            rows = self._sparql(self._advocate_general_query(wanted))
        except Exception:  # noqa: BLE001 — the caller falls back to the printed name
            return {}
        return {r["celex"]: " ".join(str(r["name"]).split())
                for r in rows if r.get("celex") and r.get("name")}

    def advocate_general(self, celex: str) -> str | None:
        """The AG for one opinion, from CELLAR. None when the endpoint has no answer (or is
        down) — the caller then falls back to the name printed on the Opinion itself."""
        return self.advocate_generals([celex]).get(celex)

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
  FILTER(REGEX(STR(?celex), "^6[0-9]{{4}}[CTF][JOVC]"))
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
        targeted = bool(self.cited_by_celex or self.legislation_celex)
        offset = 0
        pages = 0
        seen: set[str] = set()
        while True:
            if self.cited_by_celex:
                query = self._citing_query(self.cited_by_celex)
            elif self.legislation_celex:
                query = self._discover_query(since)
            else:
                query = self._enumerate_query(since, offset)
            rows = self._sparql(query)
            if not rows:
                return
            yielded = 0
            for row in rows:
                celex = row.get("celex")
                if not celex or celex in seen:
                    continue
                seen.add(celex)
                yielded += 1
                ecli = row.get("ecli")
                _doc_type, court = classify_celex(celex, row.get("rtype"))
                yield Stub(
                    stable_id=ecli or celex,  # ECLI is the primary key where present (§1.1)
                    landing_url=f"https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:{celex}",
                    raw_url=f"{CELEX_BASE}/{celex}",
                    hint_date=_parse_iso(row.get("date")),
                    # ECLI/CELEX identify the document; they are not case names. Keeping
                    # either in ``title`` prevents fetch() from deriving the parties.
                    title=row.get("title"),
                    court=court,
                    hints={
                        "celex": celex,
                        "link": row.get("link", ""),
                        "rtype": row.get("rtype", ""),
                        **({"watermark": row["date"]} if row.get("date") else {}),
                    },
                )
            pages += 1
            if (
                targeted
                or len(rows) < self.per_page
                or yielded == 0
                or (max_pages is not None and pages >= max_pages)
            ):
                return
            offset += len(rows)

    def fetch(self, stub: Stub) -> Record | None:
        celex = stub.hints.get("celex") or stub.stable_id
        doc_type, court = classify_celex(celex, stub.hints.get("rtype"))
        raw, raw_ext, text, segments, source_language = self._best_rendition(
            stub.raw_url, celex
        )

        # When the full decision is available only in French, EUR-Lex commonly publishes
        # a short English OJ information notice later.  Keep the full French reasons as
        # the authoritative body and append only that notice's operative part, visibly
        # labelled so readers never mistake the summary for a translation of the reasons.
        oj_meta: dict = {}
        if source_language == "fr" and doc_type in (DocType.JUDGMENT, DocType.DECISION):
            oj = self._english_oj_operative_part(celex)
            if oj:
                oj_text, oj_celex, oj_url = oj
                start = len(text or "")
                heading = "English Official Journal notice — operative part"
                text = f"{(text or '').rstrip()}\n\n{heading}\n\n{oj_text}".lstrip()
                segments = list(segments) + [Segment(
                    label=heading,
                    char_start=start + (2 if start else 0),
                    char_end=len(text),
                    kind="ruling",
                )]
                oj_meta = {
                    "english_oj_notice_celex": oj_celex,
                    "english_oj_notice_url": oj_url,
                    "english_oj_operative_part": oj_text,
                }
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
        # 0) a consolidated version (sector-0 CELEX ``0…-YYYYMMDD``) → its authoritative base
        # act. Deterministic from the identifier; consolidated text has no legal value, so
        # the edge is what lets a pincite reach the base act the snapshot documents (§EU).
        from ..eu_law import consolidation_base
        _cons_base = consolidation_base(celex)
        if _cons_base:
            relations.append(TypedRelation(
                relationship_type=RelationshipType.CONSOLIDATES, raw_citation_string=celex,
                dst_id=_cons_base, extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
        # 1) the typed edge to the legislation that surfaced this case (§1A).
        if self.legislation_celex:
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
            language=source_language,
            source_language=source_language,
            landing_url=stub.landing_url,
            raw_bytes=raw if raw is not None else stub.raw_url.encode(),
            raw_ext=raw_ext,
            text=text,
            segments=segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={
                "celex": celex,
                # Who delivered it. CELLAR models the AG properly, so ask it first; the name
                # printed on the Opinion's own first page is the fallback for when the
                # endpoint has no answer (older opinions) or is unreachable — and it carries
                # the delivery date either way.
                **(self._ag_meta(celex, text) if doc_type == DocType.OPINION else {}),
                **({"currency": _eu_currency_meta(celex)} if doc_type == DocType.LEGISLATION else {}),
                **("html_fallback" and {"content_format": "html"} if raw_ext == "html" else {}),
                **({"origin_country": origin_country} if origin_country else {}),
                **({"referring_courts": referring_courts} if referring_courts else {}),
                **({"language_fallback": "en-to-fr"} if source_language == "fr" else {}),
                **oj_meta,
            },
        )

    def _ag_meta(self, celex: str, text: str | None) -> dict:
        """``advocate_general`` (+ ``delivered_on`` where the text gives it) for an Opinion:
        CELLAR's structured relation first, the Opinion's printed heading as the fallback.
        ``advocate_general_source`` records which answered, so a later audit can tell a
        catalogued fact from one read off a page."""
        printed = parse_ag_opinion_head(text)
        structured = self.advocate_general(celex)
        out = {k: v for k, v in printed.items() if k != "advocate_general"}
        name = structured or printed.get("advocate_general")
        if name:
            out["advocate_general"] = name
            out["advocate_general_source"] = "cellar" if structured else "document"
        return out

    def _fetch_formex(self, url: str, language: str = "en") -> bytes | None:
        """Best-effort Formex fetch: a 404/406 (no Formex rendition) is not fatal —
        the case is still catalogued with its SPARQL metadata + edges."""
        try:
            resp = self._client.get(
                url,
                headers={"Accept": "application/zip;mtype=fmx4",
                         "Accept-Language": {"en": "eng", "fr": "fra"}.get(language, language)},
            )
        except FetchError:
            return None
        return unzip_formex(resp.content)

    def _fetch_html(self, url: str, language: str = "en") -> bytes | None:
        """HTML fallback: fetch the EUR-Lex HTML rendering when no Formex exists.
        Many pre-2010 CJEU cases have no Formex in CELLAR but do have HTML.
        The same CELLAR content-negotiation URL serves HTML with the right Accept header."""
        try:
            resp = self._client.get(
                url,
                headers={"Accept": "text/html;q=0.9,*/*;q=0.8", "Accept-Language": language},
            )
        except FetchError:
            return None
        content = resp.content
        low = content[:512].lower()
        if b"<html" in low or b"<!doctype" in low:
            return content
        return None

    def _fetch_eurlex_html(self, celex: str, language: str) -> bytes | None:
        """Fetch the public EUR-Lex rendition directly.

        CELLAR's content-negotiation endpoint can return no HTML even while the public
        EUR-Lex reader has a rendition.  In particular this occurs during the interval
        in which a newly delivered CJEU judgment exists in one language only.
        """
        url = f"https://eur-lex.europa.eu/legal-content/{language.upper()}/TXT/?uri=CELEX:{celex}"
        try:
            resp = self._client.get(
                url,
                headers={"Accept": "text/html;q=0.9,*/*;q=0.8", "Accept-Language": language},
            )
        except FetchError:
            return None
        content = resp.content
        low = content[:1024].lower()
        return content if b"<html" in low or b"<!doctype" in low else None

    def _rendition(self, url: str, celex: str, language: str):
        raw = self._fetch_formex(url, language)
        if raw is not None:
            text, segments = extract_formex(raw)
            if text:
                return raw, "xml", text, segments
        for html in (self._fetch_html(url, language),
                     self._fetch_eurlex_html(celex, language)):
            if html is None:
                continue
            text = self._html_to_text(html)
            if text and "requested document does not exist" not in text.lower():
                return html, "html", text, []
        return None

    def _best_rendition(self, url: str, celex: str):
        """English first; use French when English is absent or is a French passthrough."""
        english = self._rendition(url, celex, "en")
        if english and _rendition_language(english[2]) != "fr":
            return (*english, "en")
        french = self._rendition(url, celex, "fr")
        if french:
            return (*french, "fr")
        if english:  # retain a useful body even if language detection was uncertain
            return (*english, _rendition_language(english[2]) or "en")
        return None, "txt", None, [], "en"

    def _english_oj_operative_part(self, celex: str) -> tuple[str, str, str] | None:
        """English operative part from the OJ result notice paired with a decision."""
        m = re.match(r"^(6\d{4}[CTF])[JO](\d{4})$", celex or "", re.I)
        if not m:
            return None
        notice_celex = f"{m.group(1)}A{m.group(2)}".upper()
        url = f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{notice_celex}"
        html = self._fetch_eurlex_html(notice_celex, "en")
        text = self._html_to_text(html) if html else None
        operative = _extract_oj_operative_part(text)
        return (operative, notice_celex, url) if operative else None

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
            body = (soup.find(id="document1")
                    or soup.find(id="document-content")
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
        raw_bytes, raw_ext, text, segments, source_language = self._cellar._best_rendition(
            stub.raw_url, celex
        )
        oj_meta: dict = {}
        if source_language == "fr" and doc_type in (DocType.JUDGMENT, DocType.DECISION):
            oj = self._cellar._english_oj_operative_part(celex)
            if oj:
                oj_text, oj_celex, oj_url = oj
                start = len(text or "")
                heading = "English Official Journal notice — operative part"
                text = f"{(text or '').rstrip()}\n\n{heading}\n\n{oj_text}".lstrip()
                segments = list(segments) + [Segment(
                    label=heading, char_start=start + (2 if start else 0),
                    char_end=len(text), kind="ruling",
                )]
                oj_meta = {
                    "english_oj_notice_celex": oj_celex,
                    "english_oj_notice_url": oj_url,
                    "english_oj_operative_part": oj_text,
                }
        # Return None only when we have literally nothing — no content AND no ECLI
        # to key the record by. If we have any content, store it (metadata-only is
        # already handled by SPARQL in EUCellarAdapter; this path is targeted fetch).
        if raw_bytes is None and ecli is None:
            return None  # genuinely absent from CELLAR — let the caller report "not found"
        return Record(
            source=self.source, stable_id=ecli or celex,
            ecli=ecli, doc_type=doc_type, court=court, title=title,
            landing_url=stub.landing_url,
            language=source_language, source_language=source_language,
            raw_bytes=raw_bytes if raw_bytes is not None else celex.encode(),
            raw_ext=raw_ext,
            text=text, segments=segments, extracted_via=ExtractedVia.STRUCTURED,
            extra={"celex": celex,
                   **({"celex_aliases": list(self.celex_aliases)} if self.celex_aliases else {}),
                   **({"currency": _eu_currency_meta(celex, meta)} if doc_type == DocType.LEGISLATION else {}),
                   **("html_fallback" and {"content_format": "html"} if raw_ext == "html" else {}),
                   **({"language_fallback": "en-to-fr"} if source_language == "fr" else {}),
                   **oj_meta},
        )
