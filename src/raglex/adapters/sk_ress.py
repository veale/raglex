"""Slovakia — the Ministry of Justice RESS / InfoSúd decision register (``sk-ress``).

Slovakia publishes **every instance**, which almost nothing else in Europe does for free:
4.68 million decisions as at August 2026, from the district courts up. The corpus's other
continental case-law sources are appellate and selective; this one is the courtroom floor.

```
GET https://obcan.justice.sk/pilot/api/ress-isu-service/v1/rozhodnutie      search
GET https://obcan.justice.sk/pilot/api/ress-isu-service/v1/rozhodnutie/{id} one decision
```

No auth. An OpenAPI 3.0.1 description sits at ``/v3/api-docs``, and the parameter names
below come from it rather than from guesswork.

## Two things about this API are silent failures

**1. The date filters accept two formats and only obey one.** ``vydaniaOd`` and
``indexDatumOd`` take ISO ``YYYY-MM-DD``. Given ``DD.MM.YYYY`` — the format the API itself
uses when it *prints* ``datumVydania`` — the filter is accepted, ignored, and the response
reports the whole 4.68-million-row corpus as the result set. An adapter that trusted the
output format for its input would have walked the entire register believing it was
harvesting one week.

**2. ``page`` is one-based and echoes back one less.** ``page=1`` and ``page=0`` both
return the first page; ``page=2`` returns the second. The echoed ``page`` field is
therefore always the request minus one, which reads like an off-by-one bug in the caller
and is not.

## The list payload is a teaser

``rozhodnutieList`` carries the court, judge, file mark and date but **no ECLI, no legal
area, no referenced legislation and no text** — all four require the detail call, which
also hands back the decision as a PDF. So one detail request and one PDF fetch per
document, which is what makes this a slow source rather than a difficult one.

## What the detail call is worth

* ``ecli`` — ``ECLI:SK:NSSR:2025:6322010282.1``, the only cross-court unique identifier.
* ``odkazovanePredpisy`` — Slov-Lex ELI references **with the provision in the fragment**
  (``/SK/ZZ/2005/300/#paragraf-221.odsek-3.pismeno-a``). A fully resolved statutory
  citation: act, section, paragraph and lettered point, published as data.
* ``povaha`` — the outcome taxonomy, in paired active/passive forms: *Potvrdzujúce*
  (affirming) / *Potvrdené* (affirmed), *Zrušujúce* / *Zrušené*, *Zmeňujúce* / *Zmenené*.
* ``povodnySud`` + ``povodnaSpisovaZnacka`` — **the court below and its file mark.** This
  is the appellate edge, stated by the register rather than inferred by matching marks
  across instances, and it is what lets a Slovak appeal be traversed to the decision it
  reviewed.

## Judges are named; parties are not

``sudca.meno`` carries the judge's full name in the clear while party names are
pseudonymised. That asymmetry is the publisher's, not ours, but it is the field that
surprises people, so it is recorded explicitly rather than buried in a blob.

## The ``pilot`` path

``pilot`` is in the base URL and the Ministry's open-data page invites contact about a
production integration. Treat the base as unstable: it is defined once, here, so
re-pointing it is a one-line change.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Iterator

from ..citations.slovak import case_id, eli_to_id
from ..core.adapter import BaseAdapter, option_flag, option_int, resume_floor
from ..core.errors import FetchError
from ..extraction import ocr as _ocr
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

BASE = "https://obcan.justice.sk/pilot/api/ress-isu-service/v1"
SITE = "https://obcan.justice.sk"

#: Server-side page size. 100 is honoured; larger values are clamped to 100.
_PAGE_SIZE = 100
#: The register starts in the 1960s but is thin before the 2010s; a default that reaches
#: back a decade keeps an unqualified backfill to a few hundred thousand documents rather
#: than to four million. Override with ``start_date`` to take the whole thing.
_DEFAULT_START = "2015-01-01"

#: ``povaha_rozhodnutia`` → the appellate relation it asserts about the decision BELOW.
#: The active forms are what an appellate decision carries; the passive ones are what the
#: first-instance decision carries once it has been reviewed, so only the active side
#: mints an edge (the passive side would duplicate it from the other end).
_OUTCOME_RELATION = {
    "potvrdzujúce": RelationshipType.FOLLOWS,
    "zrušujúce": RelationshipType.OVERRULES,
    "zmeňujúce": RelationshipType.DISTINGUISHES,
}


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _listify(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _iso(value) -> date | None:
    """``datumVydania`` is ``DD.MM.YYYY`` on output — never ISO, whatever the input took."""
    text = _clean(value)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _api_date(value: str | date | None) -> str | None:
    """The ISO form the filters actually obey — see the module docstring, failure 1."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    parsed = _iso(value)
    return parsed.isoformat() if parsed else None


class SlovakRESSAdapter(BaseAdapter):
    source = "sk-ress"
    # A public ministry service serving PDFs. Two requests per document (detail + file),
    # so the floor is what keeps a large backfill from looking like a load test.
    min_interval = 0.8
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        query: str | None = None,
        court_type: str | None = None,
        court: str | None = None,
        area: str | None = None,
        outcome: str | None = None,
        legislation: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        ids: str | list[str] | None = None,
        include_text: bool | str | None = None,
        page_size: int | str | None = None,
        start_page: int | str | None = None,
        start_offset: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.query = (query or "").strip() or None
        self.court_type = (court_type or "").strip() or None
        self.court = (court or "").strip() or None
        self.area = (area or "").strip() or None
        self.outcome = (outcome or "").strip() or None
        self.legislation = (legislation or "").strip() or None
        self.start_date = (start_date or "").strip() or None
        self.end_date = (end_date or "").strip() or None
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = list(ids or [])
        self.include_text = option_flag(include_text, True)
        self.page_size = max(1, min(_PAGE_SIZE, option_int(page_size, _PAGE_SIZE)))
        self.start_page = max(1, option_int(start_page, 1))
        # Handed back by ``jobs`` from an interrupted run's checkpoint. The register
        # pages uniformly, so the offset maps straight onto a page — see
        # ``core.adapter.resume_floor`` for why it lands one page early on purpose.
        if start_offset not in (None, ""):
            floor = resume_floor(option_int(start_offset, 0), self.page_size)
            self.start_page = max(self.start_page, floor // self.page_size + 1)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            yield from self._discover_ids()
            return
        params: dict[str, object] = {"size": self.page_size}
        if self.query:
            params["query"] = self.query
        if self.court_type:
            params["typSuduFacetFilter"] = self.court_type
        if self.court:
            params["guidSud"] = self.court
        if self.area:
            params["oblastPravnejUpravyFacetFilter"] = self.area
        if self.outcome:
            params["povahaRozhodnutiaFacetFilter"] = self.outcome
        if self.legislation:
            params["odkazovanePredpisy"] = self.legislation
        if since:
            # ``indexDatumOd`` is when the register INDEXED the decision, which is the only
            # monotonic key it has: a 2018 judgment can be published in 2026, and a
            # ``vydaniaOd`` cursor would never see it. A day of overlap covers the
            # timezone the API does not state.
            cursor = _iso(since) or date.today()
            params["indexDatumOd"] = (cursor - timedelta(days=1)).isoformat()
        else:
            params["vydaniaOd"] = _api_date(self.start_date or _DEFAULT_START)
            if self.end_date:
                params["vydaniaDo"] = _api_date(self.end_date)
        page = self.start_page
        seen = 0
        total: int | None = None
        while True:
            payload = self._get(f"{BASE}/rozhodnutie", {**params, "page": page})
            rows = (payload or {}).get("rozhodnutieList") or []
            if total is None:
                total = int((payload or {}).get("numFound") or 0) or None
            if not rows:
                return
            for row in rows:
                stub = self._stub(row, feed_total=total,
                                  offset=(self.start_page - 1) * self.page_size + seen,
                                  watermark=(payload or {}).get("updateDate"))
                if stub is not None:
                    yield stub
                seen += 1
            if max_pages is not None and page - self.start_page + 1 >= max_pages:
                return
            page += 1

    def _discover_ids(self) -> Iterator[Stub]:
        """A targeted pull by composite guid, ECLI or spisová značka."""
        for ident in self.ids:
            ident = ident.strip()
            if not ident:
                continue
            # The guid is TWO uuids joined by a colon and must not be split on it.
            if re.fullmatch(r"[0-9a-f-]{36}:[0-9a-f-]{36}", ident, re.IGNORECASE):
                detail = self._get(f"{BASE}/rozhodnutie/{ident}")
                if detail:
                    yield self._stub_from_detail(detail)
                continue
            key = "ecli" if ident.upper().startswith("ECLI:") else "spisovaZnacka"
            payload = self._get(f"{BASE}/rozhodnutie", {key: ident, "page": 1, "size": 20})
            for row in (payload or {}).get("rozhodnutieList") or []:
                stub = self._stub(row)
                if stub is not None:
                    yield stub

    def _stub(self, row: dict, *, feed_total: int | None = None,
              offset: int | None = None, watermark=None) -> Stub | None:
        guid = _clean(row.get("guid"))
        if not guid:
            return None
        hints: dict = {"row": row}
        if watermark:
            hints["watermark"] = _api_date(watermark)
        if feed_total:
            hints["feed_total"] = int(feed_total)
        if offset is not None:
            hints["resume_offset"] = offset
        court = (row.get("sud") or {}).get("nazov")
        return Stub(
            stable_id=f"sk/ress/{guid.split(':', 1)[-1]}",
            landing_url=f"{SITE}/rozhodnutia/{guid}",
            title=_clean(row.get("spisovaZnacka")) or guid,
            court=_clean(court) or None,
            hint_date=_iso(row.get("datumVydania")),
            hints=hints,
        )

    def _stub_from_detail(self, detail: dict) -> Stub:
        stub = self._stub(detail)
        assert stub is not None  # a detail payload always carries its own guid
        return Stub(**{**stub.__dict__, "hints": {**stub.hints, "detail": detail}})

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        row = dict(stub.hints.get("row") or {})
        guid = _clean(row.get("guid"))
        detail = stub.hints.get("detail") or (
            self._get(f"{BASE}/rozhodnutie/{guid}") if guid else None)
        if not detail:
            return None
        row.update(detail)

        ecli = _clean(row.get("ecli"))
        court = _clean((row.get("sud") or {}).get("nazov"))
        file_mark = _clean(row.get("spisovaZnacka"))
        form = _clean(row.get("formaRozhodnutia"))
        decided = _iso(row.get("datumVydania"))

        text, needs_ocr, pdf_url, raw = None, False, None, None
        document = row.get("dokument") or {}
        if self.include_text and _clean(document.get("url")):
            pdf_url = _clean(document.get("url"))
            raw = self._bytes(pdf_url)
            if raw:
                text, needs_ocr, _spans, _engine = _ocr.text_or_ocr(raw)
        if not (text or "").strip():
            # The register publishes the metadata for decisions whose file it does not
            # serve. Those are real, citable decisions and the appellate and norm edges
            # below are the point of holding them, so a missing PDF is recorded rather
            # than being treated as a missing document.
            text = _metadata_text(row)

        relations = _norm_relations(row.get("odkazovanePredpisy"))
        relations += _appeal_relations(row)

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            ecli=ecli if ecli.upper().startswith("ECLI:") else None,
            doc_type=DocType.JUDGMENT,
            title=", ".join(x for x in (court, form, file_mark) if x) or stub.stable_id,
            court=court or None,
            decision_date=decided,
            language="sk",
            source_language="sk",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="pdf" if raw else None,
            text=text or None,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[t for t in (_clean(x) for x in
                                    (*_listify(row.get("oblast")),
                                     *_listify(row.get("podOblast")))) if t],
            extra={k: v for k, v in {
                "jurisdiction": "sk",
                "guid": guid or None,
                "spisova_znacka": file_mark or None,
                "identifikacne_cislo": _clean(row.get("identifikacneCislo")) or None,
                "forma_rozhodnutia": form or None,
                "povaha_rozhodnutia": [_clean(p) for p in _listify(row.get("povaha"))] or None,
                "oblast": [_clean(p) for p in _listify(row.get("oblast"))] or None,
                "pod_oblast": [_clean(p) for p in _listify(row.get("podOblast"))] or None,
                "court_guid": _clean((row.get("sud") or {}).get("registreGuid")) or None,
                # Named in the clear by the publisher while the parties are pseudonymised.
                # Recorded as its own field so the asymmetry is visible rather than
                # discovered later inside a text blob.
                "judge": _clean((row.get("sudca") or {}).get("meno")) or None,
                "judge_guid": _clean((row.get("sudca") or {}).get("registreGuid")) or None,
                "lower_court": _clean((row.get("povodnySud") or {}).get("nazov")) or None,
                "lower_file_mark": _clean(row.get("povodnaSpisovaZnacka")) or None,
                "pdf_url": pdf_url,
                "updated_at": _clean(row.get("updateDate")) or None,
                "needs_ocr": needs_ocr or None,
                "text_from_metadata": True if raw is None or not raw else None,
                "aliases": _aliases(file_mark, row) or None,
            }.items() if v not in (None, "", [], {})},
        )

    # -- http ----------------------------------------------------------------
    def _get(self, url: str, params: dict | None = None) -> dict | None:
        try:
            resp = self._client.get(url, params=params or {},
                                    headers={"Accept": "application/json"},
                                    raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        if resp.status_code >= 400:
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            return None

    def _bytes(self, url: str) -> bytes | None:
        try:
            resp = self._client.get(url, raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        return resp.content if resp.status_code < 400 and resp.content else None


def _aliases(file_mark: str, row: dict) -> list[str]:
    """The forms Slovak practice cites this decision by.

    The spisová značka is registered court-qualified AND bare, because a citing judgment
    writes it bare — the mark is unique within a court but not across the country, so the
    bare key is a best effort that the ECLI (which is unique) supersedes when both are
    known.
    """
    out: list[str] = []
    if file_mark:
        out.append(case_id(file_mark))
    identifier = _clean(row.get("identifikacneCislo"))
    if identifier:
        out.append(f"sk:file:{identifier}")
    return [a for a in dict.fromkeys(out) if a]


def _metadata_text(row: dict) -> str:
    """A readable record for a decision whose PDF the register does not serve."""
    lines = [
        _clean((row.get("sud") or {}).get("nazov")),
        _clean(row.get("formaRozhodnutia")),
        f"Spisová značka: {_clean(row.get('spisovaZnacka'))}"
        if _clean(row.get("spisovaZnacka")) else "",
        f"Dátum vydania: {_clean(row.get('datumVydania'))}"
        if _clean(row.get("datumVydania")) else "",
        f"ECLI: {_clean(row.get('ecli'))}" if _clean(row.get("ecli")) else "",
        "; ".join(_clean(p) for p in _listify(row.get("povaha")) if _clean(p)),
        "; ".join(_clean(p) for p in _listify(row.get("oblast")) if _clean(p)),
        *[_clean(h) for h in _listify(row.get("zvyraznenie")) if _clean(h)],
    ]
    return "\n\n".join(re.sub(r"<[^>]+>", "", line) for line in lines if line)


def _norm_relations(predpisy) -> list[TypedRelation]:
    """``odkazovanePredpisy`` → ``INTERPRETS`` edges, provision anchor included.

    These are Slov-Lex ELI paths, not prose, so the edge is ``structured``: the act, the
    section, the odsek and the písmeno all come from the publisher.
    """
    out: list[TypedRelation] = []
    seen: set[tuple[str, str]] = set()
    for item in _listify(predpisy):
        if not isinstance(item, dict):
            continue
        parsed = eli_to_id(_clean(item.get("nazov")))
        if parsed is None:
            continue
        work, anchor = parsed
        if (work, anchor or "") in seen:
            continue
        seen.add((work, anchor or ""))
        out.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=_clean(item.get("nazov")) or work,
            dst_id=work, dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING))
    return out


def _appeal_relations(row: dict) -> list[TypedRelation]:
    """``povodnySud`` + ``povodnaSpisovaZnacka`` → the edge to the decision below.

    Slovakia states the appellate relation rather than leaving it to be inferred by
    matching file marks across instances. The *type* of the edge comes from ``povaha``:
    an affirming decision follows the one below, an annulling one overrules it, a varying
    one departs from it. Where the register gives no outcome the edge is still recorded —
    as ``considers``, which asserts only that this decision reviewed that one.
    """
    lower_mark = _clean(row.get("povodnaSpisovaZnacka"))
    if not lower_mark:
        return []
    kind = RelationshipType.CONSIDERS
    for value in _listify(row.get("povaha")):
        mapped = _OUTCOME_RELATION.get(_clean(value).casefold())
        if mapped is not None:
            kind = mapped
            break
    lower_court = _clean((row.get("povodnySud") or {}).get("nazov"))
    return [TypedRelation(
        relationship_type=kind,
        raw_citation_string=" — ".join(x for x in (lower_court, lower_mark) if x),
        dst_id=case_id(lower_mark), dst_anchor=None,
        extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.PENDING)]
