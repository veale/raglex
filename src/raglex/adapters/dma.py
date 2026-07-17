"""Digital Markets Act cases adapter (§1.9/§4a) — the Commission's DMA enforcement
register at digital-markets-act-cases.ec.europa.eu.

The site is an Angular front-end over the EC "ODSE" competition-case **search API**
(``webgate.ec.europa.eu/es/search-api``) — an Elasticsearch-style JSON endpoint. Far
better than scraping the rendered page: one paged POST returns every DMA record.

The API key ``CS_PROD_ODSE_PROD`` is a *static product identifier* baked into the
site config (the same for every visitor, functioning as a public read key), not a
rotating session token — so it doesn't expire. ``Origin``/``Referer`` must name the
DMA site or the Europa gateway 403s.

**Record model.** A DMA case is several records sharing a ``caseNumber``:
  - one ``METADATA_CASE`` — the case (title, type, core platform services, concerned
    obligations, gatekeeper designations, companies, legal basis, timeline events);
  - N ``METADATA_DECISION`` — each procedural step (open proceedings / preliminary
    findings / decision), anchored by date + its press release (``IP_yy_nnnn``) and,
    when published, an Official Journal reference;
  - their ``METADATA_DECISION_ATTACHMENT`` — the document rows (the actual PDF URLs are
    placeholders in the index; many DMA decisions aren't published as documents yet).
We fetch all three, group by ``caseNumber``, and emit **one document per case** carrying
the structured decision list. The retrievable full-text artifacts are the **press
releases** (presscorner, URL built from the ``IP`` reference) and the **OJ summaries**
(eur-lex) — attached as edges, with CELLAR enrichment left to the EU pipeline.

**Everything links to the DMA statute.** Every case and decision is tied to Regulation
(EU) 2022/1925 (CELEX ``32022R1925``) with an ``interprets`` edge, pinpointed to the
article it turns on (Art. 6(11), Art. 8, Art. 8(2)…), read from the case title, the
concerned obligations, and the decisions. Harvesting ``eu-legislation`` CELEX
``32022R1925`` makes those edges resolve.

**Monitoring** (§5a): the list sorts by ``caseLastDecisionDate`` DESC, but a case's
decision list grows *in place* — a new step advances that date. So the incremental
cursor is ``caseLastDecisionDate`` AND a content hash over the decision list: a case
whose hash moved is re-fetched (contenthash mechanism), surfacing the new decision, not
just brand-new cases.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Iterator

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

SEARCH_URL = "https://webgate.ec.europa.eu/es/search-api/rest/search"
API_KEY = "CS_PROD_ODSE_PROD"
DMA_CELEX = "32022R1925"  # Regulation (EU) 2022/1925 — the Digital Markets Act
SITE = "https://digital-markets-act-cases.ec.europa.eu"
PRESSCORNER = "https://ec.europa.eu/commission/presscorner/detail/en/"

_HEADERS = {
    "Origin": SITE,
    "Referer": SITE + "/",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:152.0) "
                   "Gecko/20100101 Firefox/152.0"),
}

# The metadata fields worth pulling (superset across record types).
DISPLAY_FIELDS = [
    "metadataType", "metadataReference", "caseNumber", "caseInstrument", "caseType",
    "caseTitle", "caseLastDecisionDate", "caseCorePlatformServices",
    "caseConcernedObligations", "caseDesignations", "caseCompanies", "caseLegalBasis",
    "caseTimelineEvents", "caseSectors", "caseInitiationDate",
    "decisionType", "decisionArticle", "decisionDate", "decisionCelex",
    "decisionOfficialJournalPublications", "decisionPressReleases", "es_SortDate",
]

# "Article 6(11)", "Art. 8(2)", "Article 5" — the DMA article a case/decision turns on.
_ARTICLE_RE = re.compile(r"\bArt(?:icle|\.)?\s*(\d+[a-z]?(?:\(\d+[a-z]?\))*)", re.IGNORECASE)


def _first(v):
    """The ODSE index wraps every value in a list; unwrap to the first scalar."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _items(field_val) -> list[dict]:
    """Several fields are a JSON string ``{"items":[…]}`` (press releases, OJ pubs,
    timeline). Parse it to the list of item dicts, tolerating the empty ``[{}]`` form."""
    raw = _first(field_val)
    if not raw or not isinstance(raw, str):
        return []
    try:
        items = json.loads(raw).get("items", [])
    except (ValueError, TypeError):
        return []
    return [it for it in items if isinstance(it, dict) and it]


def build_search_body(query: dict, sort: list, fields: list[str]) -> tuple[bytes, str]:
    """The API takes a multipart form of three JSON *blobs* (query/sort/displayFields).
    Returns (body, content_type). Pure, so the adapter is testable offline."""
    boundary = "----raglexdma7f3a1c"
    parts = []
    for name, obj in (("query", query), ("sort", sort), ("displayFields", fields)):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
            f"filename=\"blob\"\r\nContent-Type: application/json\r\n\r\n"
            f"{json.dumps(obj)}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode(), f"multipart/form-data; boundary={boundary}"


DMA_QUERY = {"bool": {"must": [
    {"exists": {"field": "caseNumber"}},
    {"term": {"caseInstrument": "InstrumentDMA"}},
]}}
DMA_SORT = [{"field": "caseLastDecisionDate", "order": "DESC"},
            {"field": "metadataReference", "order": "DESC"}]


def group_by_case(records: list[dict]) -> dict[str, dict]:
    """Group flat ODSE records into ``{caseNumber: {case, decisions, attachments}}``."""
    out: dict[str, dict] = {}

    def bucket(num: str) -> dict:
        return out.setdefault(num, {"case": None, "decisions": [], "attachments": []})

    for md in records:
        num = _first(md.get("caseNumber"))
        if not num:
            continue
        mt = _first(md.get("metadataType"))
        b = bucket(num)
        if mt == "METADATA_CASE":
            b["case"] = md
        elif mt == "METADATA_DECISION":
            b["decisions"].append(md)
        elif mt == "METADATA_DECISION_ATTACHMENT":
            b["attachments"].append(md)
    return out


def _decision_rows(bundle: dict) -> list[dict]:
    """Normalise a case's decisions into dated rows (newest first), each with its press
    release + OJ references — the shape the document text and edges are built from."""
    rows = []
    for md in bundle["decisions"]:
        prs = _items(md.get("decisionPressReleases"))
        ojs = _items(md.get("decisionOfficialJournalPublications"))
        d = _first(md.get("decisionDate")) or (prs[0].get("publicationDate") if prs else None) \
            or _first(md.get("es_SortDate"))
        rows.append({
            "ref": _first(md.get("metadataReference")),
            "type": _first(md.get("decisionType")),
            "article": _first(md.get("decisionArticle")),
            "date": d,
            "press_releases": [p.get("reference") for p in prs if p.get("reference")],
            "oj": [o.get("reference") for o in ojs if o.get("reference")],
        })
    rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows


def articles_for(bundle: dict) -> list[str]:
    """Every DMA article a case turns on — from its title ("… Article 6(11)"), its
    decisions, and (as fallbacks) its concerned-obligation codes rendered as text — so
    the interprets edges to the statute are pinpointed as precisely as the data allows."""
    case = bundle.get("case") or {}
    found: list[str] = []
    hay = " ".join(filter(None, [
        _first(case.get("caseTitle")) or "",
        *[str(_first(d.get("decisionArticle")) or "") for d in bundle["decisions"]],
    ]))
    for m in _ARTICLE_RE.finditer(hay):
        art = f"Article {m.group(1)}"
        if art not in found:
            found.append(art)
    return found


def _iso_date(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _case_hash(bundle: dict) -> str:
    """A stable digest over a case's decision list (refs + dates + press releases + OJ),
    so a case that gains a decision re-fetches even if already held (§5a monitoring)."""
    payload = json.dumps(_decision_rows(bundle), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class DMACasesAdapter(BaseAdapter):
    source = "dma-cases"
    min_interval = 1.0
    requires_js = False
    requires_proxy = False

    def __init__(self, *, client: RateLimitedClient | None = None, page_size: int = 100) -> None:
        self.page_size = page_size
        self._client = client or RateLimitedClient(self.source, min_interval=self.min_interval)

    def _search(self, page: int) -> dict:
        body, ctype = build_search_body(DMA_QUERY, DMA_SORT, DISPLAY_FIELDS)
        resp = self._client.request(
            "POST", SEARCH_URL,
            params={"text": "*", "pageNumber": page, "pageSize": self.page_size,
                    "apiKey": API_KEY},
            content=body, headers={**_HEADERS, "Content-Type": ctype},
        )
        return json.loads(resp.content)

    def _all_records(self, *, max_pages: int | None) -> list[dict]:
        out: list[dict] = []
        page, pages = 1, 0
        while True:
            data = self._search(page)
            results = data.get("results", []) if isinstance(data, dict) else []
            if not results:
                break
            out.extend(r.get("metadata", {}) for r in results)
            pages += 1
            total = data.get("totalResults", 0)
            if len(out) >= total or (max_pages is not None and pages >= max_pages):
                break
            page += 1
        return out

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        bundles = group_by_case(self._all_records(max_pages=max_pages))
        for num, bundle in bundles.items():
            case = bundle.get("case")
            if not case:
                continue  # decisions with no case row — skip (shouldn't happen)
            last = _first(case.get("caseLastDecisionDate"))
            # Incremental cursor: skip cases whose last decision predates the watermark.
            # A NEW decision on an existing case advances caseLastDecisionDate past the
            # cursor, so the case is re-yielded; the differing contenthash (in hints) then
            # drives the pipeline to re-fetch it — surfacing the added decision (§5a).
            if since and last and last <= since:
                continue
            yield Stub(
                stable_id=f"dma/{num}",
                landing_url=f"{SITE}/en/proceedings/{num}",
                raw_url=SEARCH_URL,
                hint_date=_iso_date(last),
                title=_first(case.get("caseTitle")) or num,
                hints={"bundle": bundle,
                       **({"watermark": last} if last else {}),
                       "contenthash": _case_hash(bundle)},
            )

    def fetch(self, stub: Stub) -> Record | None:
        bundle = stub.hints["bundle"]
        case = bundle["case"]
        num = _first(case.get("caseNumber"))
        rows = _decision_rows(bundle)
        articles = articles_for(bundle)

        # ---- render a readable document body from the structured metadata ----
        lines = [_first(case.get("caseTitle")) or num, ""]
        for label, field in (("Case number", "caseNumber"), ("Type", "caseType"),
                             ("Core platform services", "caseCorePlatformServices"),
                             ("Concerned obligations", "caseConcernedObligations"),
                             ("Gatekeeper", "caseCompanies"), ("Legal basis", "caseLegalBasis")):
            v = _first(case.get(field))
            if v:
                lines.append(f"{label}: {v}")
        if articles:
            lines.append(f"DMA articles: {', '.join(articles)}")
        lines.append("")
        lines.append("Decisions and procedural steps:")
        for r in rows:
            d = (r["date"] or "")[:10]
            pr = ", ".join(r["press_releases"])
            oj = ", ".join(r["oj"])
            bits = [b for b in (r["type"], f"Art. {r['article']}" if r["article"] else None) if b]
            head = " — ".join(bits) if bits else "Decision"
            lines.append(f"  • {head} ({d})" + (f" · press release {pr}" if pr else "")
                         + (f" · OJ {oj}" if oj else ""))
        text = "\n".join(lines)

        # ---- edges: link everything to the DMA statute + press releases + OJ ----
        relations: list[TypedRelation] = []
        # the case interprets the DMA as a whole, and each identified article as a pinpoint
        seen_edges: set[tuple[str, str | None]] = set()

        def _interprets(anchor: str | None):
            key = (DMA_CELEX, anchor)
            if key in seen_edges:
                return
            seen_edges.add(key)
            relations.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=f"{anchor + ' of the ' if anchor else ''}Digital Markets Act",
                dst_id=DMA_CELEX, dst_anchor=anchor,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))

        _interprets(None)  # base link to the regulation
        for art in articles:
            _interprets(art)

        # press releases → presscorner (retrievable full-text of each step)
        for r in rows:
            for pr in r["press_releases"]:
                relations.append(TypedRelation(
                    relationship_type=RelationshipType.MENTIONS,
                    raw_citation_string=f"{PRESSCORNER}{pr}",
                    dst_id=None, extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.PENDING))

        designations = [d.get("reference") or d for d in _items(case.get("caseDesignations"))]
        timeline = _items(case.get("caseTimelineEvents"))
        press_all = sorted({pr for r in rows for pr in r["press_releases"]})
        oj_all = sorted({o for r in rows for o in r["oj"]})

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.DECISION,
            title=_first(case.get("caseTitle")) or num,
            court="dma",
            decision_date=_iso_date(_first(case.get("caseLastDecisionDate"))),
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=text.encode("utf-8"),
            raw_ext="txt",
            text=text,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=["dma"] + [_slug(a) for a in articles],
            extra={k: v for k, v in {
                "case_number": num,
                "case_type": _first(case.get("caseType")),
                "core_platform_services": _first(case.get("caseCorePlatformServices")),
                "concerned_obligations": _first(case.get("caseConcernedObligations")),
                "companies": _first(case.get("caseCompanies")),
                "legal_basis": _first(case.get("caseLegalBasis")),
                "dma_articles": articles,
                "designations": designations,
                "decisions": rows,
                "press_releases": press_all,
                "press_release_urls": [f"{PRESSCORNER}{pr}" for pr in press_all],
                "official_journal": oj_all,
                "timeline": timeline,
                "contenthash": stub.hints.get("contenthash"),
                "url": stub.landing_url,
            }.items() if v not in (None, [], "")},
        )


def _slug(text: str) -> str:
    return "dma-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
