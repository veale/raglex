"""Sweden — Domstolsverket's *Sök rättspraxis* (``se-domstol``).

The Swedish courts' own case-law service, issued as open data on 31 March 2025. It is a
**precedent layer**, not a corpus: 17,321 publications, superior courts only, selected for
what guides other courts.

* **Case reports (referat): 1981 onwards** — the classic NJA / RÅ / AD / MÖD series.
* **Judgments and decisions: 3 March 2025 onwards** — full text, but only from then.

So it will not move the corpus's size. What it adds is three things that would otherwise
have to be inferred, and one — the Supreme Court's own case names — that could not be
inferred at all.

```
GET https://rattspraxis.etjanst.domstol.se/api/v1/publiceringar   list (paged)
GET .../api/v1/publiceringar/{id}                                 one publication
GET .../api/v1/publiceringar/grupp/{gruppKorrelationsnummer}       the linked family
GET .../api/v1/domstolar                                          the court code registry
GET .../api/v1/bilagor/{lagringId}                                the PDF
```

No auth, no key.

## Paging: the page is zero-based and the sort parameter is a trap

``page`` starts at **0**, and ``pagesize`` caps at 100. Walking ``page=0…173`` at
``pagesize=100`` enumerates all 17,321 publications, newest decision date first, with no
gaps — verified by collecting the ids and counting them.

``sortorder``/``asc`` are accepted and **do not order the result set**: at ``pagesize=3``
the first page's newest ``publiceringstid`` is 2026-08-06 and at ``pagesize=100`` it is
2026-07-01, which cannot both be true of one global ordering. Whatever they sort, it is
not the thing being paged, so an early-stop built on them would stop in the wrong place.
This adapter therefore never sends them and never early-stops on publication time.

``publicerad_fran_och_med`` is similarly unreliable: given 2026-07-01 it returns eight
referat and omits the judgments published the same week. It is not used.

## Which is why keep-current is a full walk

The default order is by **decision** date, and Sweden publishes with a lag of up to twelve
years — a 2014 Supreme Court decision was published in July 2026. A cursor on the decision
date would never see it. So a watch re-walks all 174 pages (a few seconds of requests) and
keeps what is new by ``publiceringstid``. That is the honest cost of a source with no
usable publication-order cursor.

## No ECLI anywhere

Sweden has not joined the ECLI cooperation. Identity is therefore the service's own record
UUID, with the **report citation** (``NJA 2020 s. 123``) and the **målnummer**
(``Ö 4337-25``) registered as aliases — those are what Swedish practice actually cites, and
what ``citations.swedish`` mints.

## Three fields do work you would otherwise pay for

* ``typ`` — a native precedential-weight taxonomy: ``PREJUDIKAT`` /
  ``VAGLEDANDE_MEN_EJ_PREJUDICERANDE`` (guiding but not precedent-setting) /
  ``EJ_VAGLEDANDE`` / ``PROVNINGSTILLSTAND`` (leave to appeal).
* ``lagrumLista`` — statutory citations already parsed, with the SFS number split out.
* ``hanvisadePubliceringarLista`` — cited authorities, including CJEU judgments whose
  ECLIs are in the free text, and sometimes a ``gruppKorrelationsnummer`` that resolves to
  another record in this same corpus. Its ``fritext`` is **sometimes a JSON array encoded
  as a string** (``"[\\"NJA 1991 s. 277\\",\\"NJA 1991:47\\"]"``), so it is double-parsed.

## ``innehall`` is usually absent

In the sampled pages it was populated for the HFD record and missing from the HD and MMOD
ones, which carried only ``sammanfattning`` plus a PDF. PDF extraction is the default path
here, not the fallback.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import quote

from ..citations.swedish import act_id, case_id
from ..core.adapter import BaseAdapter, option_flag, option_int
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
from ..extraction import ocr as _ocr
from ..formats.domstol_html import parse_domstol_html

BASE = "https://rattspraxis.etjanst.domstol.se/api/v1"
SITE = "https://rattspraxis.etjanst.domstol.se/sok"

#: The service's cap. Larger values are clamped to 100 rather than rejected.
_PAGE_SIZE = 100

#: ``typ`` → how much weight the courts themselves give the publication. Kept as the
#: source's own vocabulary in ``extra`` and mirrored into tags, because "guiding but not
#: precedent-setting" is a distinction Swedish practice makes and no other source in the
#: corpus states.
_WEIGHT = {
    "PREJUDIKAT": "prejudikat",
    "VAGLEDANDE_MEN_EJ_PREJUDICERANDE": "vägledande men ej prejudicerande",
    "EJ_VAGLEDANDE": "ej vägledande",
    "PROVNINGSTILLSTAND": "prövningstillstånd",
    "FORHANDSAVGORANDE": "förhandsavgörande",
}
#: The report-series citation forms a ``hanvisadePubliceringarLista`` entry may carry.
_REPORT_RE = re.compile(
    r"(?<![\w])(?P<series>NJA|HFD|RÅ|RH|AD|MÖD|MD|MIG|PMÖD|PMD)\s+(?P<year>(?:19|20)\d{2})"
    r"\s*(?:s\.\s*(?P<page>\d{1,4})|ref\.?\s*(?P<ref>\d{1,4})|nr\s*(?P<nr>\d{1,4})"
    r"|:\s*(?P<num>\d{1,4}))")
#: A CJEU citation inside that free text: Sweden prints the ECLI, which is a clean join.
_EU_ECLI_RE = re.compile(r"(?<![\w])ECLI:EU:[CT]:\d{4}:\d{1,4}(?![\w])")
_EU_CASE_RE = re.compile(r"(?<![\w])(?P<kind>[CT])[-‑](?P<number>\d{1,4})/(?P<year>\d{2})"
                         r"(?![\w])")


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _listify(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(_clean(value)[:10])
    except ValueError:
        return None


def _fritext(entry: dict) -> list[str]:
    """``hanvisadePubliceringarLista[].fritext`` → the citations it actually holds.

    The field is normally one citation string, and sometimes a JSON array *encoded as a
    string*. Parsing it defensively is the difference between one reference and five.
    """
    raw = _clean(entry.get("fritext"))
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return [raw]
        return [_clean(x) for x in parsed if _clean(x)]
    return [raw]


class SwedishCaseLawAdapter(BaseAdapter):
    source = "se-domstol"
    min_interval = 0.4
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        court: str | None = None,
        weight: str | None = None,
        publication_form: str | None = None,
        case_number: str | None = None,
        ids: str | list[str] | None = None,
        include_documents: bool | str | None = None,
        page_size: int | str | None = None,
        start_page: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        self.court = (court or "").strip().upper() or None
        self.weight = (weight or "").strip().upper() or None
        self.publication_form = (publication_form or "").strip().upper() or None
        self.case_number = (case_number or "").strip() or None
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = list(ids or [])
        self.include_documents = option_flag(include_documents, True)
        self.page_size = max(1, min(_PAGE_SIZE, option_int(page_size, _PAGE_SIZE)))
        self.start_page = max(0, option_int(start_page, 0))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)
        self._courts: dict[str, str] | None = None

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            yield from self._discover_ids()
            return
        params: dict[str, object] = {"pagesize": self.page_size}
        if self.court:
            params["domstolkod"] = self.court
        if self.weight:
            params["publiceringstyper"] = self.weight
        if self.publication_form:
            params["publiceringsformer"] = self.publication_form
        if self.case_number:
            params["malnummer"] = self.case_number
        cutoff = _clean(since)[:19] if since else None
        page = self.start_page
        seen = 0
        while True:
            rows = self._list({**params, "page": page})
            if not rows:
                return
            for row in rows:
                # A full walk with a client-side publication cursor — see the module
                # docstring. The service's own ``publicerad_fran_och_med`` filter and its
                # sort parameters both disagree with the paged result set, so the only
                # trustworthy cursor is the one applied to what the walk actually returns.
                published = _clean(row.get("publiceringstid"))[:19]
                if cutoff and published and published <= cutoff:
                    seen += 1
                    continue
                stub = self._stub(row, offset=seen)
                if stub is not None:
                    yield stub
                seen += 1
            if len(rows) < self.page_size:
                return
            page += 1
            if max_pages is not None and page - self.start_page >= max_pages:
                return

    def _discover_ids(self) -> Iterator[Stub]:
        for ident in self.ids:
            ident = ident.strip()
            if not ident:
                continue
            if re.fullmatch(r"[0-9a-f-]{36}", ident, re.IGNORECASE):
                row = self._get(f"{BASE}/publiceringar/{ident}")
                if row:
                    stub = self._stub(row)
                    if stub is not None:
                        yield stub
                continue
            for row in self._list({"malnummer": ident, "pagesize": self.page_size,
                                   "page": 0}):
                stub = self._stub(row)
                if stub is not None:
                    yield stub

    def _stub(self, row: dict, *, offset: int | None = None) -> Stub | None:
        record_id = _clean(row.get("id"))
        if not record_id:
            return None
        marks = [_clean(m) for m in _listify(row.get("malNummerLista")) if _clean(m)]
        hints = {"row": row, "watermark": _clean(row.get("publiceringstid"))[:19] or None}
        if offset is not None:
            hints["resume_offset"] = offset
        return Stub(
            stable_id=f"se/domstol/{record_id}",
            landing_url=f"{SITE}/avgorande/{record_id}",
            title=_clean(row.get("benamning")) or (marks[0] if marks else record_id),
            court=_clean((row.get("domstol") or {}).get("domstolNamn")) or None,
            hint_date=_iso(row.get("avgorandedatum")),
            hints=hints,
        )

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        row = dict(stub.hints.get("row") or {})
        record_id = _clean(row.get("id"))
        # The list payload already carries every field the single-record route returns,
        # so the detail call is only worth making when the list omitted the body — which
        # it does for the records that have one.
        if not _clean(row.get("innehall")) and record_id:
            detail = self._get(f"{BASE}/publiceringar/{record_id}")
            if detail:
                row.update(detail)

        html = row.get("innehall") or ""
        parsed = parse_domstol_html(html) if html else None
        text = (parsed.text if parsed else None) or None
        segments = list(parsed.segments) if parsed else []
        raw, ext, needs_ocr, pdf_url = None, None, False, None
        if not text and self.include_documents:
            raw, pdf_url = self._attachment(row)
            if raw:
                text, needs_ocr, _spans, _engine = _ocr.text_or_ocr(raw)
                ext = "pdf"
        if not (text or "").strip():
            # A referat with no full text is still a real publication: the summary, the
            # statutory citations and the cited authorities are the whole point of the
            # 1981–2025 layer, which has no judgment text at all.
            text = _summary_text(row)
        if raw is None and html:
            raw, ext = html.encode("utf-8"), "html"

        marks = [_clean(m) for m in _listify(row.get("malNummerLista")) if _clean(m)]
        reports = [_clean(r) for r in _listify(row.get("referatNummerLista")) if _clean(r)]
        court = _clean((row.get("domstol") or {}).get("domstolNamn"))
        weight = _clean(row.get("typ"))

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=_title(row, court, marks, reports),
            court=court or None,
            decision_date=_iso(row.get("avgorandedatum")),
            language="sv",
            source_language="sv",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext=ext,
            text=text,
            segments=segments,
            relations=_lagrum_relations(row) + _reference_relations(row),
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[t for t in ([_clean(k) for k in _listify(row.get("nyckelordLista"))]
                                    + [_clean(k) for k in _listify(row.get("rattsomradeLista"))]
                                    + [_WEIGHT.get(weight, "")]) if t],
            extra={k: v for k, v in {
                "jurisdiction": "se",
                "record_id": record_id or None,
                "court_code": _clean((row.get("domstol") or {}).get("domstolKod")) or None,
                # The Supreme Court's own quoted case name — "Sökordslistan", "Pärmen" —
                # which is what Swedish practitioners say when they cite the case. No
                # other source in the corpus publishes a court's chosen case name.
                "case_name": _clean(row.get("benamning")) or None,
                "mal_nummer": marks or None,
                "referat_nummer": reports or None,
                "precedential_weight": weight or None,
                "publication_form": _clean(row.get("publiceringsform")) or None,
                "summary": _clean(row.get("sammanfattning")) or None,
                "keywords": [_clean(k) for k in _listify(row.get("nyckelordLista"))
                             if _clean(k)] or None,
                "legal_areas": [_clean(k) for k in _listify(row.get("rattsomradeLista"))
                                if _clean(k)] or None,
                "eu_law_flags": [_clean(k) for k in
                                 _listify(row.get("europarattsligaAvgorandenLista"))
                                 if _clean(k)] or None,
                "preparatory_works": [_clean(k) for k in _listify(row.get("forarbeteLista"))
                                      if _clean(k)] or None,
                "literature": [_clean(k) for k in _listify(row.get("litteraturLista"))
                               if _clean(k)] or None,
                # Domstolsverket groups the referat, the judgment and the leave-to-appeal
                # decision of ONE case under a shared correlation number. It is the key
                # that ties this record to its siblings, and to the records that cite it.
                "group_id": _clean(row.get("gruppKorrelationsnummer")) or None,
                "published_at": _clean(row.get("publiceringstid")) or None,
                "pdf_url": pdf_url,
                "needs_ocr": needs_ocr or None,
                "zones": (parsed.metadata.get("zones") if parsed else None) or None,
                "aliases": _aliases(marks, reports, row) or None,
            }.items() if v not in (None, "", [], {})},
        )

    # -- http ----------------------------------------------------------------
    def _list(self, params: dict) -> list[dict]:
        payload = self._get(f"{BASE}/publiceringar", params)
        return [row for row in _listify(payload) if isinstance(row, dict)]

    def _get(self, url: str, params: dict | None = None):
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

    def _attachment(self, row: dict) -> tuple[bytes | None, str | None]:
        """The publication's PDF. The storage id contains slashes and must be encoded
        whole — ``/api/v1/bilagor/190%2F9a%2F6f%2F…`` — or the route reads it as a path."""
        for item in _listify(row.get("bilagaLista")):
            storage = _clean((item or {}).get("fillagringId"))
            if not storage:
                continue
            url = f"{BASE}/bilagor/{quote(storage, safe='')}"
            try:
                resp = self._client.get(url, raise_for_4xx=False)
            except FetchError as exc:
                if exc.transient:
                    raise
                continue
            if resp.status_code < 400 and resp.content:
                return resp.content, url
        return None, None

    def courts(self) -> dict[str, str]:
        """The service's own court-code registry, ``HDO`` → ``Högsta domstolen``.

        Read from ``/domstolar`` rather than kept here: ``domstolKod`` is an internal
        code, the list runs to some thirty courts including several that no longer sit,
        and the publisher is the only authority on what each one is called.
        """
        if self._courts is None:
            rows = _listify(self._get(f"{BASE}/domstolar"))
            self._courts = {_clean(r.get("domstolKod")): _clean(r.get("domstolNamn"))
                            for r in rows if isinstance(r, dict) and r.get("domstolKod")}
        return self._courts


def _title(row: dict, court: str, marks: list[str], reports: list[str]) -> str:
    name = _clean(row.get("benamning")).strip('"“”')
    parts = [p for p in (court, reports[0] if reports else None,
                         marks[0] if marks else None) if p]
    head = ", ".join(parts)
    return f"{head} ({name})" if name and head else name or head or "Avgörande"


def _aliases(marks: list[str], reports: list[str], row: dict) -> list[str]:
    """The forms Swedish practice cites this publication by.

    Sweden mints no ECLI, so these are the only stable identifiers there are: the report
    citation ("NJA 2020 s. 123") for the 1981-onwards referat layer and the målnummer
    ("Ö 4337-25") for everything the courts themselves file by.
    """
    out: list[str] = []
    for report in reports:
        if m := _REPORT_RE.search(report):
            out.append(_report_id(m))
    for mark in marks:
        out.append(f"se:mal:{mark}")
    if group := _clean(row.get("gruppKorrelationsnummer")):
        out.append(f"se:grupp:{group}")
    return [a for a in dict.fromkeys(out) if a]


def _report_id(m: re.Match[str]) -> str:
    number = m.group("page") or m.group("ref") or m.group("nr") or m.group("num")
    return case_id(m.group("series"), m.group("year"), number)


def _summary_text(row: dict) -> str:
    """A readable record for a publication with no full text — the whole 1981–2025 layer."""
    lines = [
        _clean(row.get("benamning")),
        _clean(row.get("sammanfattning")),
        "; ".join(_clean(k) for k in _listify(row.get("nyckelordLista")) if _clean(k)),
        "; ".join(_clean((g or {}).get("referens"))
                  for g in _listify(row.get("lagrumLista")) if _clean((g or {}).get("referens"))),
    ]
    return "\n\n".join(line for line in lines if line)


def _lagrum_relations(row: dict) -> list[TypedRelation]:
    """``lagrumLista`` → ``INTERPRETS`` edges on the SFS Work.

    Domstolsverket has already parsed the citation and split out the SFS number, so this
    is structured data rather than a regex over prose. The section, where the reference
    states one, becomes the anchor in the form ``citations.swedish`` mints.
    """
    out: list[TypedRelation] = []
    seen: set[tuple[str, str]] = set()
    for item in _listify(row.get("lagrumLista")):
        if not isinstance(item, dict):
            continue
        reference = _clean(item.get("referens"))
        sfs = _clean(item.get("sfsNummer"))
        if not sfs:
            continue
        m = re.fullmatch(r"(?P<year>(?:1[6-9]|20)\d{2}):(?P<number>\d{1,5})", sfs)
        if not m:
            continue
        work = act_id(m.group("year"), m.group("number"))
        anchor = _lagrum_anchor(reference)
        if (work, anchor or "") in seen:
            continue
        seen.add((work, anchor or ""))
        out.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=reference or sfs, dst_id=work, dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING))
    return out


_LAGRUM_ANCHOR_RE = re.compile(
    r"^(?:(?P<chapter>\d{1,3})\s*kap\.?\s*)?(?P<section>\d{1,4}\s*[a-z]?)\s*§")


def _lagrum_anchor(reference: str) -> str | None:
    m = _LAGRUM_ANCHOR_RE.match(_clean(reference))
    if not m:
        return None
    section = f"{re.sub(r'\\s+', ' ', m.group('section')).strip()} §"
    return f"{m.group('chapter')} kap. {section}" if m.group("chapter") else section


def _reference_relations(row: dict) -> list[TypedRelation]:
    """``hanvisadePubliceringarLista`` → the authorities this publication cites.

    Three kinds arrive in one list and each resolves differently: a Swedish report
    citation ("NJA 1970 s. 274") to the report id the grammar mints, a CJEU judgment to
    its ECLI (Sweden prints them, which is a clean join into the EU corpus), and a
    ``gruppKorrelationsnummer`` to another record of this same service.
    """
    out: list[TypedRelation] = []
    seen: set[str] = set()

    def add(dst: str, raw: str) -> None:
        if not dst or dst in seen:
            return
        seen.add(dst)
        out.append(TypedRelation(
            relationship_type=RelationshipType.CONSIDERS,
            raw_citation_string=raw, dst_id=dst, dst_anchor=None,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING))

    for entry in _listify(row.get("hanvisadePubliceringarLista")):
        if not isinstance(entry, dict):
            continue
        group = _clean(entry.get("gruppKorrelationsnummer"))
        for citation in _fritext(entry):
            if ecli := _EU_ECLI_RE.search(citation):
                add(ecli.group(0), citation)
                continue
            if m := _REPORT_RE.search(citation):
                add(_report_id(m), citation)
                continue
            if eu := _EU_CASE_RE.search(citation):
                # "C-644/20" with no ECLI beside it — the CJEU case number, which the EU
                # corpus holds under its own key; left as the raw form so the resolver
                # can match it rather than being converted into a CELEX guess here.
                add(f"{eu.group('kind')}-{eu.group('number')}/{eu.group('year')}", citation)
                continue
            if group:
                add(f"se:grupp:{group}", citation)
    return out
