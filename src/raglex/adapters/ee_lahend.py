"""Estonia — lahend.ee, because Riigi Teataja has the duty but not the interface.

Estonian court decisions are published in the electronic Riigi Teataja — Supreme Court
since 1993, first and second instance since 2001 — under a duty in the Public Information
Act. There is no API and no bulk download for them: the entry point is an HTML page, and
RIK's open-data path for the court-decisions dataset 404s.

**lahend.ee** has already done the work. A non-profit (part of the nimistu.ee family of
Estonian open-data services) has extracted **3.05 million citations from the text of
346,000 decisions** and joined them to the Riigi Teataja legislation corpus and to
EUR-Lex, and it exposes the result free and without an account through a Model Context
Protocol server:

```
POST https://lahend.ee/api/lahend-mcp/mcp     JSON-RPC 2.0, Streamable HTTP
```

That is an unusual transport for a harvest adapter, and it is used here because it is the
**only** public interface: ``lahend.ee/api`` itself 404s, and the MCP endpoint is what the
service documents. The protocol is ordinary JSON-RPC over POST; the response arrives as
``text/event-stream`` with the payload on a ``data:`` line, which is the only wrinkle.

## What this buys that a Riigi Teataja scraper would not

* ``search_rulings`` — court, case type, area of law and a **decision-date range**, with
  ``offset``, so the corpus can be walked month by month and resumed.
* ``fetch`` — the full decision as Markdown, plus its metadata.
* ``sections_cited_by_ruling`` — **the provisions the decision relies on**, resolved to an
  act abbreviation and a §, with the lõiked. Extracted from the text by lahend.ee and
  joined to the statute, which is precisely the layer the corpus would otherwise have to
  build from prose.
* ``eu_law_cited_by_ruling`` — **the EU instruments it cites, as CELEX numbers with the
  articles**. An Estonian decision citing Article 6 GDPR arrives already joined to
  32016R0679; nothing else in this batch of five sources hands that over pre-resolved.

## What is a selection, and it matters

The English EU e-Justice page for Estonia says "usually all final judgments are
published"; the Estonian version of the same page answers "all decisions or only a
selection?" with **"Ainult valik"** — only a selection — and sets out the filter: the
judgment must have entered into force, and in civil and administrative cases must contain
no sensitive personal data, with names replaced by initials and privacy not substantially
prejudiced. Treat the corpus as a filtered selection, not a census.

## Names

Natural persons are anonymised by the court before publication (the decisions read
"Xi kaebus", "Y esitas"). Company names are not, and lahend.ee joins them to the business
register, which is why a decision carries a ``companies`` list.

## Incremental

``search_rulings`` filters on the **decision** date, and Estonian publication lags it.
A watch therefore re-walks a trailing window rather than cutting at the cursor — the same
shape the Austrian adapter uses, and for the same reason.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Iterator

from ..citations.estonian import act_key, case_family_id, case_id
from ..core.adapter import BaseAdapter, option_flag, option_int, resume_floor, resume_floor
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
from ..formats.lahend_md import parse_lahend_md

ENDPOINT = "https://lahend.ee/api/lahend-mcp/mcp"
SITE = "https://lahend.ee"

#: ``search_rulings`` caps its page at 25 and takes an ``offset``, so a month is walked in
#: pages of 25 rather than requested whole.
_LIMIT = 25
#: The corpus reaches back to the 2001 publication duty for the lower courts; the Supreme
#: Court's own material starts in 1993.
_EARLIEST = date(1993, 1, 1)
#: ``case_type`` values the service accepts, and the Estonian name of each.
CASE_TYPES = {"civil": "Tsiviilasi", "administrative": "Haldusasi", "criminal": "Kriminaalasi"}


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _iso(value) -> date | None:
    text = _clean(value)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def parse_sse(body: str) -> dict | None:
    """The JSON payload of a Streamable-HTTP response.

    The server answers ``text/event-stream`` even for a single-shot call, so the JSON sits
    on a ``data:`` line rather than being the body. A plain ``resp.json()`` fails on every
    call, which is the one thing about this transport that has to be handled explicitly.
    """
    for line in (body or "").splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except (json.JSONDecodeError, ValueError):
                continue
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None


class EstonianLahendAdapter(BaseAdapter):
    source = "ee-lahend"
    # A volunteer-run non-profit service. The floor is deliberately generous: three calls
    # per document (fetch + two citation lookups) at this rate is a considerate crawl.
    min_interval = 1.2
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        query: str | None = None,
        court: str | None = None,
        case_type: str | None = None,
        category: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        ids: str | list[str] | None = None,
        include_citations: bool | str | None = None,
        lookback_days: int | str | None = None,
        start_offset: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.query = (query or "").strip() or None
        self.court = (court or "").strip() or None
        self.case_type = (case_type or "").strip().lower() or None
        if self.case_type and self.case_type not in CASE_TYPES:
            raise ValueError(f"case_type must be one of {', '.join(CASE_TYPES)}")
        self.category = (category or "").strip() or None
        self.start_date = (start_date or "").strip() or None
        self.end_date = (end_date or "").strip() or None
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = list(ids or [])
        self.include_citations = option_flag(include_citations, True)
        self.lookback_days = max(1, option_int(lookback_days, 90))
        # Handed back by ``jobs`` from an interrupted run's checkpoint — see
        # ``core.adapter.resume_floor``. An adapter that reports ``resume_offset`` and
        # cannot take it back raises TypeError on resume, and the retry is filed as done.
        self.start_offset = resume_floor(option_int(start_offset, 0), _LIMIT)
        self._seen = 0
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)
        self._rpc_id = 0

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            for ident in self.ids:
                ident = ident.strip()
                if ident:
                    yield Stub(stable_id=case_id(ident), title=ident,
                               hints={"ruling_id": ident if ident.startswith("ruling:")
                                      else None, "case_number": ident})
            return
        today = date.today()
        if since:
            cursor = _iso(since) or today
            windows = [(cursor - timedelta(days=self.lookback_days), today)]
        else:
            start = _iso(self.start_date) or _EARLIEST
            end = _iso(self.end_date) or today
            windows = list(_months(start, end))
        # ``_seen`` counts every ruling the walk passes; ``emitted`` counts the ones
        # actually yielded. They are only the same on a fresh run. On a resume the
        # cursor has to be measured against what was WALKED — counting yields would
        # leave the offset at zero for the whole crawl, because nothing below the
        # checkpoint is yielded, and a resumed run would then emit nothing at all.
        self._seen = 0
        emitted = 0
        for lo, hi in windows:
            for stub in self._walk(lo, hi):
                yield stub
                emitted += 1
            if max_pages is not None and emitted >= max_pages * _LIMIT:
                return

    def _walk(self, lo: date, hi: date) -> Iterator[Stub]:
        arguments: dict[str, object] = {
            "date_from": lo.isoformat(), "date_to": hi.isoformat(), "limit": _LIMIT}
        if self.query:
            arguments["query"] = self.query
        if self.court:
            arguments["court"] = self.court
        if self.case_type:
            arguments["case_type"] = self.case_type
        if self.category:
            arguments["category"] = self.category
        offset = 0
        total: int | None = None
        while True:
            payload = self._call("search_rulings", {**arguments, "offset": offset})
            rows = (payload or {}).get("rulings") or []
            if total is None:
                total = int((payload or {}).get("total") or 0) or None
            if not rows:
                return
            for row in rows:
                stub = self._stub(row, feed_total=total, offset=self._seen)
                # Resuming: the listing still has to be walked — a month's size is not
                # known until it is asked for, so pages cannot be skipped blind without
                # risking an unbounded loop — but nothing below the checkpoint is
                # fetched. The saving is the 50,000 documents, not the listing.
                if stub is not None and self._seen >= self.start_offset:
                    yield stub
                self._seen += 1
                offset += 1
            if len(rows) < _LIMIT:
                return

    def _stub(self, row: dict, *, feed_total: int | None = None,
              offset: int | None = None) -> Stub | None:
        ruling = _clean(row.get("id"))
        number = _clean(row.get("caseNo"))
        if not ruling and not number:
            return None
        hints: dict = {"ruling_id": ruling or None, "row": row, "case_number": number}
        if feed_total:
            hints["feed_total"] = int(feed_total)
        if offset is not None:
            hints["resume_offset"] = offset
        return Stub(
            stable_id=case_id(number) if number else f"ee/lahend/{ruling.split(':')[-1]}",
            landing_url=_clean(row.get("url")) or None,
            title=number or ruling,
            court=_clean(row.get("court")) or None,
            hint_date=_iso(row.get("date")),
            hints=hints,
        )

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        ruling = _clean(stub.hints.get("ruling_id"))
        if not ruling:
            number = _clean(stub.hints.get("case_number"))
            found = self._call("search_rulings", {"query": f'"{number}"', "limit": 5})
            for row in (found or {}).get("rulings") or []:
                if _clean(row.get("caseNo")) == number:
                    ruling = _clean(row.get("id"))
                    break
            if not ruling:
                return None
        payload = self._call("fetch", {"id": ruling})
        if not payload:
            return None
        parsed = parse_lahend_md(_clean_markdown(payload.get("text")))
        if not (parsed.text or "").strip():
            return None

        meta = parsed.metadata or {}
        row = dict(stub.hints.get("row") or {})
        number = (_clean(meta.get("case_number")) or _clean(row.get("caseNo"))
                  or _clean(stub.hints.get("case_number")))
        court = _clean(meta.get("court")) or _clean(row.get("court"))
        category = _clean(row.get("category")) or _clean(meta.get("category"))
        decided = _iso(meta.get("decided_at")) or _iso(row.get("date")) or stub.hint_date

        relations: list[TypedRelation] = []
        sections: list[dict] = []
        eu_cited: list[dict] = []
        if self.include_citations:
            sections = ((self._call("sections_cited_by_ruling", {"id": ruling}) or {})
                        .get("sections") or [])
            eu_cited = ((self._call("eu_law_cited_by_ruling", {"id": ruling}) or {})
                        .get("cited") or [])
            relations = _section_relations(sections) + _eu_relations(eu_cited)

        return Record(
            source=self.source,
            stable_id=case_id(number) if number else stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=" — ".join(x for x in (category or None, number or None) if x)
                  or _clean(payload.get("title")) or stub.stable_id,
            court=court or None,
            decision_date=decided,
            language="et",
            source_language="et",
            landing_url=_clean(payload.get("url")) or stub.landing_url,
            text=parsed.text,
            segments=parsed.segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[t for t in re.split(r"\s*›\s*", category) if t] if category else [],
            extra={k: v for k, v in {
                "jurisdiction": "ee",
                "lahend_id": ruling,
                "case_number": number or None,
                "case_kind": _clean(meta.get("case_kind")) or None,
                "procedure": _clean(meta.get("procedure")) or None,
                "status": _clean(meta.get("status")) or None,
                "category": category or None,
                "judge": _clean(meta.get("judge")) or None,
                # Natural persons are anonymised by the court; companies are not, and
                # lahend.ee joins them to the business register. Recorded explicitly so
                # the asymmetry is visible.
                "companies": [_clean(c) for c in (row.get("companies") or []) if _clean(c)]
                             or None,
                "cited_sections": sections or None,
                "cited_eu_law": eu_cited or None,
                "zones": meta.get("zones") or None,
                # Estonia publishes a SELECTION filtered by the statutory redaction rules,
                # not the whole docket — see the module docstring.
                "coverage": "selection",
                "aliases": _aliases(number) or None,
            }.items() if v not in (None, "", [], {})},
        )

    # -- JSON-RPC ------------------------------------------------------------
    def _call(self, tool: str, arguments: dict) -> dict | None:
        self._rpc_id += 1
        body = {"jsonrpc": "2.0", "id": self._rpc_id, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}
        try:
            resp = self._client.request(
                "POST", ENDPOINT, json=body,
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        if resp.status_code >= 400:
            return None
        payload = parse_sse(resp.text)
        result = (payload or {}).get("result") or {}
        if result.get("isError"):
            return None
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        # Older MCP servers answer with text content only; the same JSON is in there.
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item.get("text") or "")
                except (json.JSONDecodeError, ValueError):
                    continue
        return None


def _months(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Month windows, newest first.

    Newest first because that is the order a partial run should cover: an interrupted
    backfill has then harvested the most recent law, and the frontier resumes at the month
    it stopped in rather than at 1993.
    """
    cursor = date(end.year, end.month, 1)
    floor = date(start.year, start.month, 1)
    while cursor >= floor:
        nxt = (date(cursor.year + 1, 1, 1) if cursor.month == 12
               else date(cursor.year, cursor.month + 1, 1))
        yield max(cursor, start), min(nxt - timedelta(days=1), end)
        cursor = (date(cursor.year - 1, 12, 1) if cursor.month == 1
                  else date(cursor.year, cursor.month - 1, 1))


_MD_HEADER_RE = re.compile(r"^\s*#\s+.*$", re.MULTILINE)


def _clean_markdown(text) -> str:
    return str(text or "")


def _aliases(number: str) -> list[str]:
    """The case WITHOUT its document suffix, so a citation of the case reaches whichever
    document of it the corpus holds. ``3-25-3458/5`` and ``3-25-3458/7`` are two documents
    of one case, and a judgment citing "3-25-3458" means the case."""
    if not number:
        return []
    family = case_family_id(number)
    return [family] if family != case_id(number) else []


def _section_relations(sections: list) -> list[TypedRelation]:
    """``sections_cited_by_ruling`` → ``INTERPRETS`` edges on the Estonian statute.

    lahend.ee resolves the reference to an act and a §; the ``lgs`` it returns are the
    lõiked the decision actually invoked, which is a finer anchor than the § alone. The
    documented limitation is that a citation resolves to the paragraph and not below it,
    so "§ 37 lõike 4" links to § 37 — the lõige is therefore recorded on the anchor from
    ``lgs`` rather than being invented from the text.
    """
    out: list[TypedRelation] = []
    seen: set[tuple[str, str]] = set()
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        act = _clean(section.get("act"))
        paragraph = _clean(section.get("paragrahv"))
        if not act or not paragraph:
            continue
        lgs = [_clean(x) for x in (section.get("lgs") or []) if _clean(x)]
        anchors = [f"§ {paragraph} lg {lg}" for lg in lgs] or [f"§ {paragraph}"]
        target = act_key(act)
        for anchor in anchors:
            if (target, anchor) in seen:
                continue
            seen.add((target, anchor))
            out.append(TypedRelation(
                relationship_type=RelationshipType.INTERPRETS,
                raw_citation_string=f"{act} {anchor}", dst_id=target, dst_anchor=anchor,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
    return out


def _eu_relations(cited: list) -> list[TypedRelation]:
    """``eu_law_cited_by_ruling`` → edges straight onto the CELEX the corpus already holds.

    This is the field that makes lahend.ee worth using rather than scraping Riigi Teataja:
    the EU instrument arrives as a CELEX with the articles the decision cited, so an
    Estonian judgment joins the EU corpus without a grammar pass.
    """
    out: list[TypedRelation] = []
    seen: set[tuple[str, str]] = set()
    for item in cited or []:
        if not isinstance(item, dict):
            continue
        celex = _clean(item.get("celex"))
        if not celex:
            continue
        articles = [_clean(a) for a in (item.get("articles") or []) if _clean(a)]
        anchors = [f"Article {a}" for a in articles] or [""]
        kind = (RelationshipType.CONSIDERS if _clean(item.get("kind")) == "case"
                else RelationshipType.INTERPRETS)
        for anchor in anchors:
            if (celex, anchor) in seen:
                continue
            seen.add((celex, anchor))
            out.append(TypedRelation(
                relationship_type=kind,
                raw_citation_string=_clean(item.get("label")) or celex,
                dst_id=celex, dst_anchor=anchor or None,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
    return out
