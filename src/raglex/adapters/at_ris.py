"""Austria — the RIS OGD Judikatur API (``at-*``).

The Rechtsinformationssystem des Bundes publishes every Austrian court and tribunal
decision it holds through one open, unauthenticated JSON API, and it publishes something
almost nothing else in Europe does for free: **a treatment-typed citation graph**.

```
GET https://data.bka.gv.at/ris/api/v2.6/Judikatur?Applikation=Justiz&…
```

Fifteen *Applikationen* partition the corpus by deciding body, and all fifteen are live
(~985,000 documents as at August 2026): ``Justiz`` (OGH, OLG, LG, BG — 138k), ``Vwgh``
(357k), ``Bvwg`` (288k), ``Lvwg`` (77k), ``AsylGH`` (53k), ``Uvs`` (26k), ``Vfgh`` (24k),
``Verg`` (8k), ``Dok`` (5k), ``Ubas`` (4k), ``Pvak`` (3k), **``Dsk`` (1.9k — the
Datenschutzbehörde)**, ``Gbk`` (1k), ``Bks`` and ``Umse``. ``Bfg`` is *not* one of them:
the Bundesfinanzgericht publishes separately through findok.bmf.gv.at.

## Two document types, and only one of them is a judgment

* ``JJT_``/``…T_`` — an *Entscheidungstext*, the decision itself.
* ``JJR_``/``JWR_``/``…R_`` — a **Rechtssatz**: one legal proposition, abstracted by the
  court's own documentation office, given a permanent number (``RS0042418``), its own
  ECLI, and a list of every decision that has since applied it. Austrian practitioners
  cite the proposition — "RIS-Justiz RS0042418" — more often than the case.

The Rechtssatz is what makes this a citator rather than a corpus. Each entry in
``Justiz.Entscheidungstexte`` carries the applying decision's docket and date plus two
controlled vocabularies:

* ``Entscheidungsart`` — the disposition, including ``Verstärkter Senat``, the enlarged
  panel that is Austria's overruling mechanism.
* ``Anmerkung`` — the treatment, in an editorial shorthand: absent means applied,
  ``auch`` also, ``vgl``/``vgl auch`` cf., ``nur T5`` only that sub-proposition,
  ``Beisatz wie T7`` same rider, ``Gegenteilig`` contrary, and
  ``Ablehnung von 3 Ob 110/14y`` an express rejection **naming the decision rejected**.

Those become typed relations (``applies`` / ``considers`` / ``distinguishes`` /
``overrules``) instead of something a classifier has to guess from prose. The rejection
edge's endpoint is the *named prior decision*, and the applying decision that did the
rejecting is preserved in ``raw_citation_string`` — the fact belongs to that decision,
but the only place Austria publishes it is on the Rechtssatz.

## Four things about this API that will cost you a run

1. **Errors arrive as HTTP 200.** A bad parameter returns ``200`` with
   ``OgdSearchResult.Error`` and no ``OgdDocumentResults``. Unchecked, that is silently
   "zero hits" — see ``_hits``.
2. **A Rechtssatz's ``Entscheidungsdatum`` is the date of the most recent applying
   decision, not of the proposition.** A 1954 Rechtssatz in the sample carried
   ``2026-05-26``. It is therefore the right key for a date-sliced *sweep* (the record
   moves into the window when it changes) and the wrong value for ``decision_date``,
   which is taken from the originating decision in the ID.
3. **XML→JSON collapse.** With one result ``OgdDocumentReference`` is an object, not an
   array — likewise ``Entscheidungstexte.item`` and ``Normen.item``. And the degenerate
   cases differ *within one record*: ``Geschaeftszahl.item`` is a semicolon-delimited
   **string** even with forty entries, while ``Normen.item`` is a string when singular
   and a list when plural.
4. **``DokumenteProSeite`` is an enum**, not an integer: ``Ten``, ``Twenty``, ``Fifty``,
   ``OneHundred``. ``Hundred`` is a schema validation error.

## Windows, not deep pages

Discovery slices by ``EntscheidungsdatumVon``/``Bis`` and splits a window that holds more
than the paging depth can safely reach, rather than paging into the tens of thousands.
Each window reports its own ``Hits``, so the split is driven by what the service says is
there — see ``_windows``. A backfill therefore resumes at a date rather than at an
offset, which is also what makes it restartable after the worker is replaced.

The keep-current path re-polls a trailing window and keeps whatever ``Allgemein.Geaendert``
says has changed since the cursor. It has to be a window rather than a point: RIS
publishes a decision days to months after it is given, and anonymisation revisions land
on old documents, so a cursor on the decision date alone would strand both.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Iterator

from ..citations.austrian import case_id, collection_id, norm_citations, rechtssatz_id
from ..core.adapter import BaseAdapter, option_int, resume_floor
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
from ..formats.ris_xml import parse_ris

API = "https://data.bka.gv.at/ris/api/v2.6/Judikatur"
SITE = "https://www.ris.bka.gv.at"

#: Every ``Applikation`` the Judikatur endpoint accepts, with the body it holds and
#: whether that body is a court. Taken from the schema-validation error the service
#: returns on a bad value, then confirmed one at a time against live ``Hits``.
APPLICATIONS: dict[str, tuple[str, bool]] = {
    "Justiz": ("Ordentliche Gerichtsbarkeit (OGH, OLG, LG, BG)", True),
    "Vwgh": ("Verwaltungsgerichtshof", True),
    "Vfgh": ("Verfassungsgerichtshof", True),
    "Bvwg": ("Bundesverwaltungsgericht", True),
    "Lvwg": ("Landesverwaltungsgerichte", True),
    "AsylGH": ("Asylgerichtshof (2008–2013)", True),
    "Dsk": ("Datenschutzbehörde", False),
    "Gbk": ("Gleichbehandlungskommission", False),
    "Verg": ("Vergabekontrolle", False),
    "Dok": ("Disziplinarkommissionen", False),
    "Pvak": ("Personalvertretungsaufsichtsbehörde", False),
    "Uvs": ("Unabhängige Verwaltungssenate (bis 2013)", False),
    "Ubas": ("Unabhängiger Bundesasylsenat (bis 2008)", False),
    "Umse": ("Umweltsenat (bis 2013)", False),
    "Bks": ("Bundeskommunikationssenat (bis 2013)", False),
}

#: The page-size enum. ``Hundred`` is rejected by the schema; ``OneHundred`` is the max.
_PAGE_SIZE = "OneHundred"
_PER_PAGE = 100
#: How deep a single date window may be paged before it is split instead. Deep paging
#: works (page 100 returns data), but a window that needs hundreds of pages is a window
#: that cannot be resumed cheaply, and every page is a request against a SOAP backend.
_MAX_PAGES_PER_WINDOW = 30
#: RIS's own oldest decision dates from the 1940s; the Rechtssatz corpus reaches 1954.
_EARLIEST_YEAR = 1945

#: ``Anmerkung`` shorthand → the edge it asserts between the proposition and the decision.
#: Absent means the decision applied the proposition straightforwardly, which is the
#: majority case and the default.
_TREATMENT = (
    (re.compile(r"(?i)\bAblehnung\s+von\b"), RelationshipType.OVERRULES),
    (re.compile(r"(?i)\bGegenteilig\b"), RelationshipType.DISTINGUISHES),
    (re.compile(r"(?i)\bvgl\b"), RelationshipType.CONSIDERS),
)
#: The decision an ``Ablehnung von …`` names — an Austrian docket, in the spaced form.
_ABLEHNUNG_RE = re.compile(
    r"(?i)Ablehnung\s+von\s+(?P<docket>\d{1,2}\s?(?:ObA|ObS|OCg|Ok|Ob|Os|Ns|Nc|Bs|Ds)"
    r"\s?\d{1,4}/\d{2}[a-z]?)")
#: ``Entscheidungsart`` values that mean the court sat as an enlarged panel — the only
#: formation that can depart from settled OGH authority.
_ENLARGED_PANEL = re.compile(r"(?i)verst(?:ä|ae)rkter\s+Senat")

#: Keyword fields RIS separates in three different ways, sometimes within one value.
_KEYWORD_SPLIT = re.compile(r"<br\s*/?>|[\r\n]+|\s*;\s*|\s*,\s{1,}")


def _listify(value) -> list:
    """The XML→JSON collapse: one child is an object, several are an array."""
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _items(field) -> list:
    """``{"item": …}`` → a list, whichever degenerate shape it arrived in."""
    if isinstance(field, dict):
        return _listify(field.get("item"))
    return _listify(field)


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _iso(value) -> date | None:
    text = _clean(value)[:10]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        # RIS carries a handful of typo'd dates ("3001-04-25" on a Vorarlberg UVS
        # decision). A date the Republic could not have produced is not a date.
        if 1900 <= parsed.year <= date.today().year + 1:
            return parsed
        return None
    return None


def _keywords(*values) -> list[str]:
    out: list[str] = []
    for value in values:
        for item in _listify(value):
            for part in _KEYWORD_SPLIT.split(str(item or "")):
                token = " ".join(part.split())
                if token and token not in out:
                    out.append(token)
    return out


#: ``JJR_19540519_OGH0002_0010OB00346_5400000_002`` — the originating date is the first
#: field of every RIS document number. On a Rechtssatz that is the ONLY place the
#: proposition's own date survives (see the module docstring, gotcha 2).
_ID_DATE_RE = re.compile(r"_(?P<date>(?:18|19|20)\d{6})_")


def originating_date(document_id: str) -> date | None:
    m = _ID_DATE_RE.search(document_id or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group("date"), "%Y%m%d").date()
    except ValueError:
        return None


class AustrianRISAdapter(BaseAdapter):
    """One RIS ``Applikation``. Registered once per body — see ``adapters.registry``."""

    min_interval = 0.6  # a SOAP backend behind a government site, not a CDN
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        application: str | None = "Justiz",
        source_key: str | None = None,
        document_type: str | None = None,
        court: str | None = None,
        query: str | None = None,
        norm: str | None = None,
        ids: str | list[str] | None = None,
        earliest_year: int | str | None = None,
        lookback_days: int | str | None = None,
        start_date: str | None = None,
        start_offset: int | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        # A SourceOption arrives as whatever the form sent, including ``None`` for
        # anything the user never touched — and ``at-ris`` exposes ``application`` as an
        # option, so the untouched case is the common one. None means the default.
        self.application = _canonical_application(application or "Justiz")
        self.source = source_key or f"at-{self.application.casefold()}"
        self.document_type = (document_type or "").strip() or None
        self.court = (court or "").strip() or None
        self.query = (query or "").strip() or None
        self.norm = (norm or "").strip() or None
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = list(ids or [])
        self.earliest_year = max(1900, option_int(earliest_year, _EARLIEST_YEAR))
        # The trailing window a keep-current run re-polls. RIS publishes late and
        # revises old documents, so a cursor on the decision date alone strands both.
        self.lookback_days = max(1, option_int(lookback_days, 120))
        self.start_date = (start_date or "").strip() or None
        # Handed back by ``jobs`` from an interrupted run's checkpoint — see
        # ``core.adapter.resume_floor``. Without this keyword the resume raises TypeError
        # and the retry is recorded as done.
        self.start_offset = resume_floor(option_int(start_offset, 0), _PER_PAGE)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90)

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            yield from self._discover_ids()
            return
        today = date.today()
        if since:
            cursor = _iso(since) or today
            start = cursor - timedelta(days=self.lookback_days)
            windows = [(start, today + timedelta(days=1))]
            changed_after = _clean(since)[:10]
        else:
            start = _iso(self.start_date) or date(self.earliest_year, 1, 1)
            windows = [(start, today + timedelta(days=1))]
            changed_after = None
        emitted = 0
        for lo, hi in windows:
            for stub in self._walk_window(lo, hi, changed_after=changed_after,
                                          max_pages=max_pages, emitted=emitted):
                yield stub
                emitted += 1
                if max_pages is not None and emitted >= max_pages * _PER_PAGE:
                    return

    def _walk_window(self, lo: date, hi: date, *, changed_after: str | None,
                     max_pages: int | None, emitted: int) -> Iterator[Stub]:
        for window_lo, window_hi, total in self._windows(lo, hi):
            pages = min(_MAX_PAGES_PER_WINDOW,
                        max(1, (total + _PER_PAGE - 1) // _PER_PAGE))
            for page in range(1, pages + 1):
                # Resuming: a page wholly below the checkpoint is skipped without being
                # requested. The window walk is deterministic — the splits are driven by
                # the API's own Hits — so counting past it reaches the same place the
                # interrupted run had got to, at one request per window rather than per
                # document.
                if emitted + _PER_PAGE <= self.start_offset:
                    emitted += _PER_PAGE
                    continue
                payload = self._search(window_lo, window_hi, page=page)
                refs = _references(payload)
                if not refs:
                    break
                for ref in refs:
                    stub = self._stub(ref, changed_after=changed_after,
                                      feed_total=total, offset=emitted)
                    if stub is not None:
                        yield stub
                        emitted += 1

    def _windows(self, lo: date, hi: date) -> Iterator[tuple[date, date, int]]:
        """Date windows small enough to enumerate, discovered by asking the API.

        A window whose ``Hits`` exceed what ``_MAX_PAGES_PER_WINDOW`` can reach is split
        in half and each half re-counted, down to a single day. Driving the split off the
        service's own count is what keeps a 357,000-document application enumerable
        without guessing at a slice size that happens to work for one court and truncates
        another.
        """
        pending: list[tuple[date, date]] = [(lo, hi)]
        ceiling = _MAX_PAGES_PER_WINDOW * _PER_PAGE
        while pending:
            window_lo, window_hi = pending.pop()
            total = _hits(self._search(window_lo, window_hi, page=1))
            if total <= 0:
                continue
            if total <= ceiling or (window_hi - window_lo).days <= 1:
                yield window_lo, window_hi, total
                continue
            middle = window_lo + (window_hi - window_lo) / 2
            # Pushed newest-last so ``pop`` walks oldest→newest: a backfill that is
            # interrupted has then covered a contiguous stretch of history, which is a
            # resumable state, rather than a scatter of windows.
            pending.append((window_lo, middle))
            pending.append((middle, window_hi))

    def _discover_ids(self) -> Iterator[Stub]:
        """A targeted pull by RIS Dokumentnummer, ECLI or Geschäftszahl."""
        for ident in self.ids:
            ident = ident.strip()
            if not ident:
                continue
            if ident.upper().startswith("ECLI:"):
                params = {"Suchworte": ident}
            elif re.match(r"^[A-Z]{2,8}[TR]?_", ident):
                params = {"Dokumentnummer": ident}
            else:
                params = {"Geschaeftszahl": ident}
            payload = self._get(params)
            for ref in _references(payload):
                stub = self._stub(ref, changed_after=None)
                if stub is not None:
                    yield stub

    def _search(self, lo: date, hi: date, *, page: int) -> dict:
        params = {
            "EntscheidungsdatumVon": lo.isoformat(),
            "EntscheidungsdatumBis": (hi - timedelta(days=1)).isoformat(),
            "DokumenteProSeite": _PAGE_SIZE,
            "Seitennummer": str(page),
        }
        if self.document_type:
            params["Dokumenttyp"] = self.document_type
        if self.query:
            params["Suchworte"] = self.query
        if self.norm:
            params["Norm"] = self.norm
        if self.court:
            params[f"{self.application}.Gericht"] = self.court
        return self._get(params)

    def _get(self, params: dict) -> dict:
        query = {"Applikation": self.application, **params}
        try:
            resp = self._client.get(API, params=query,
                                    headers={"Accept": "application/json"},
                                    raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return {}
        if resp.status_code >= 400:
            return {}
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        result = (payload or {}).get("OgdSearchResult") or {}
        error = result.get("Error")
        if error:
            # Gotcha 1: a rejected parameter is an HTTP 200. Treating it as an empty
            # result set would silently record "this window holds nothing".
            raise FetchError(
                f"{self.source}: RIS rejected the query: "
                f"{_clean(error.get('Message'))[:200]}", transient=False)
        return result

    def _stub(self, ref: dict, *, changed_after: str | None,
              feed_total: int | None = None, offset: int | None = None) -> Stub | None:
        data = (ref or {}).get("Data") or {}
        meta = data.get("Metadaten") or {}
        technical = meta.get("Technisch") or {}
        general = meta.get("Allgemein") or {}
        judicature = meta.get("Judikatur") or {}
        document_id = _clean(technical.get("ID"))
        if not document_id:
            return None
        changed = _clean(general.get("Geaendert"))[:10]
        if changed_after and changed and changed <= changed_after:
            return None
        ecli = _clean(judicature.get("EuropeanCaseLawIdentifier"))
        hints = {"data": data, "watermark": changed or None}
        if feed_total:
            hints["feed_total"] = int(feed_total)
        if offset is not None:
            hints["resume_offset"] = offset
        dockets = _dockets(judicature.get("Geschaeftszahl"))
        return Stub(
            stable_id=stable_id_for(ecli, document_id),
            landing_url=_clean(general.get("DokumentUrl")) or None,
            title=(dockets[0] if dockets else document_id),
            court=_clean(technical.get("Organ")) or None,
            hint_date=originating_date(document_id) or _iso(
                judicature.get("Entscheidungsdatum")),
            hints=hints,
        )

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        data = stub.hints.get("data") or {}
        meta = data.get("Metadaten") or {}
        technical = meta.get("Technisch") or {}
        general = meta.get("Allgemein") or {}
        judicature = meta.get("Judikatur") or {}
        application = _clean(technical.get("Applikation")) or self.application
        specific = judicature.get(application) or judicature.get(self.application) or {}
        document_id = _clean(technical.get("ID"))

        xml_url = _content_url(data, "Xml")
        if not xml_url:
            return None
        try:
            raw = self._client.get(xml_url).content
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        parsed = parse_ris(raw)
        if not (parsed.text or "").strip():
            return None

        doc_type_label = _clean(judicature.get("Dokumenttyp"))
        is_rechtssatz = doc_type_label.casefold() == "rechtssatz"
        dockets = _dockets(judicature.get("Geschaeftszahl"))
        rs_numbers = [_clean(n) for n in _items(specific.get("Rechtssatznummern"))
                      if _clean(n)]
        if not rs_numbers and _clean(parsed.metadata.get("rechtssatz_number")):
            rs_numbers = [_clean(parsed.metadata["rechtssatz_number"])]
        court = (_clean(specific.get("Gericht")) or _clean(specific.get("EntscheidendeBehoerde"))
                 or _clean(specific.get("Kommission")) or _clean(technical.get("Organ"))
                 or APPLICATIONS.get(application, (application, True))[0])
        decided = originating_date(document_id) or _iso(judicature.get("Entscheidungsdatum"))
        collection = _clean(parsed.metadata.get("collection_number"))

        relations = _norm_relations(judicature.get("Normen"))
        treatments: list[dict] = []
        if is_rechtssatz:
            edges, treatments = _rechtssatz_relations(specific)
            relations += edges
        relations += _stammrechtssatz_relations(specific)

        title = _title(court, doc_type_label, dockets, rs_numbers,
                       specific.get("Leitsatz") or specific.get("Kurzinformation"))
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            ecli=_ecli(judicature),
            doc_type=(DocType.JUDGMENT
                      if APPLICATIONS.get(application, ("", True))[1] else DocType.DECISION),
            title=title,
            court=court,
            decision_date=decided,
            language="de",
            source_language="de",
            landing_url=_clean(general.get("DokumentUrl")) or stub.landing_url,
            raw_bytes=raw,
            raw_ext="xml",
            text=parsed.text,
            segments=parsed.segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=_keywords(judicature.get("Schlagworte"),
                                 specific.get("Indizes"),
                                 specific.get("Rechtsgebiete")),
            extra={k: v for k, v in {
                "jurisdiction": "at",
                "ris_id": document_id,
                "application": application,
                "document_type": doc_type_label or None,
                "is_rechtssatz": True if is_rechtssatz else None,
                "geschaeftszahl": dockets or None,
                "rechtssatz_numbers": rs_numbers or None,
                "disposition": _clean(specific.get("Entscheidungsart")) or None,
                "panel": _clean(specific.get("Senat")) or None,
                "authority": _clean(specific.get("EntscheidendeBehoerde")) or None,
                "commission": _clean(specific.get("Kommission")) or None,
                "bundesland": _clean(specific.get("Bundesland")) or None,
                "leitsatz": _clean(specific.get("Leitsatz")) or None,
                "kurzinformation": _clean(specific.get("Kurzinformation")) or None,
                "discrimination_ground": _clean(specific.get("Diskriminierungsgrund")) or None,
                "discrimination_type":
                    _clean(specific.get("Diskriminierungstatbestand")) or None,
                "author": _clean(specific.get("Verfasser")) or None,
                # The Dsk/Pvak "Anfechtung" note is the only statement RIS makes about
                # whether a decision is final; a data-protection decision under appeal to
                # the BVwG is not yet the law and must not read as if it were.
                "finality": _clean(specific.get("Anfechtung")) or None,
                "access": _clean(specific.get("Zugang")) or None,
                "collection_number": collection or None,
                "full_decision_url": _clean(judicature.get("GesamteEntscheidungUrl")) or None,
                "rechtssaetze_url": _clean(judicature.get("RechtssaetzeUrl")) or None,
                "rechtssatzkette_url": _clean(specific.get("RechtssatzketteUrl")) or None,
                "content_urls": _content_urls(data) or None,
                "treatments": treatments or None,
                "zones": parsed.metadata.get("zones") or None,
                "changed_at": _clean(general.get("Geaendert")) or None,
                "published_at": _clean(general.get("Veroeffentlicht")) or None,
                "aliases": _aliases(document_id, dockets, rs_numbers, collection,
                                    application, is_rechtssatz) or None,
            }.items() if v not in (None, "", [], {})},
        )


# ── identity ────────────────────────────────────────────────────────────────
def stable_id_for(ecli: str | None, document_id: str) -> str:
    """The id a RIS document is held under — its ECLI, else the RIS document number.

    Austria mints an ECLI for almost everything, including for Rechtssätze
    (``ECLI:AT:OGH0002:2021:RS0133477``), which is unusual and is what lets an Austrian
    decision dedupe against any other source that carries the same ECLI.
    """
    ecli = _clean(ecli)
    if ecli.upper().startswith("ECLI:"):
        return ecli
    return f"at/ris/{_clean(document_id)}"


def _ecli(judicature: dict) -> str | None:
    ecli = _clean(judicature.get("EuropeanCaseLawIdentifier"))
    return ecli if ecli.upper().startswith("ECLI:") else None


def _canonical_application(value: str) -> str:
    wanted = _clean(value).casefold()
    for name in APPLICATIONS:
        if name.casefold() == wanted:
            return name
    raise ValueError(
        f"unknown RIS Applikation {value!r}; known: {', '.join(APPLICATIONS)}")


def _dockets(field) -> list[str]:
    """``Geschaeftszahl.item`` → the dockets it names.

    Gotcha 3: this field is a **semicolon-delimited string** even when it names forty
    decisions, while ``Normen.item`` beside it is a list. Splitting only on the JSON
    shape gave a Rechtssatz one 900-character "docket".
    """
    out: list[str] = []
    for item in _items(field):
        for part in str(item or "").split(";"):
            token = _clean(part)
            if token and token not in out:
                out.append(token)
    return out


def _aliases(document_id: str, dockets: list[str], rs_numbers: list[str],
             collection: str, application: str, is_rechtssatz: bool) -> list[str]:
    """Every form the corpus may cite this document by.

    A **Rechtssatz** gets its RS number and nothing else: its ``Geschaeftszahl`` lists
    every decision that has applied the proposition, so registering those dockets against
    it would make each of those judgments resolve to the headnote instead of to itself.
    An **Entscheidungstext** gets its own docket, which is exactly the form a later
    judgment writes.
    """
    aliases = [f"at/ris/{document_id}"] if document_id else []
    if is_rechtssatz:
        aliases += [rechtssatz_id(n) for n in rs_numbers]
    else:
        court = _ALIAS_COURT.get(application, application.upper())
        aliases += [case_id(court, d) for d in dockets]
        if collection and application in ("Vfgh", "Vwgh"):
            aliases.append(collection_id(
                "VfSlg" if application == "Vfgh" else "VwSlg", collection))
    return [a for a in dict.fromkeys(aliases) if a]


#: The court token ``citations.austrian.case_id`` mints from a docket's own shape, so an
#: alias registered here and a citation recognised there meet.
_ALIAS_COURT = {"Justiz": "OGH", "Vwgh": "VWGH", "Vfgh": "VFGH", "Bvwg": "BVWG",
                "Lvwg": "LVWG"}


def _title(court: str, doc_type: str, dockets: list[str], rs_numbers: list[str],
           summary) -> str:
    head = ", ".join(x for x in (court, doc_type or None) if x)
    if rs_numbers:
        return ", ".join(x for x in (head, "/".join(rs_numbers)) if x)
    if dockets:
        return ", ".join(x for x in (head, dockets[0]) if x)
    return head or _clean(summary)[:120] or "RIS"


# ── payload navigation ──────────────────────────────────────────────────────
def _references(result: dict) -> list[dict]:
    results = (result or {}).get("OgdDocumentResults") or {}
    return _listify(results.get("OgdDocumentReference"))


def _hits(result: dict) -> int:
    """The window's TOTAL count — ``Hits.#text``, not the page length."""
    hits = ((result or {}).get("OgdDocumentResults") or {}).get("Hits") or {}
    try:
        return int(_clean(hits.get("#text")) or 0)
    except ValueError:
        return 0


def _content_urls(data: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for reference in _listify((data or {}).get("Dokumentliste", {}).get("ContentReference")):
        for url in _listify(((reference or {}).get("Urls") or {}).get("ContentUrl")):
            kind = _clean((url or {}).get("DataType"))
            href = _clean((url or {}).get("Url"))
            if kind and href:
                out.setdefault(kind, href)
    return out


def _content_url(data: dict, kind: str) -> str | None:
    return _content_urls(data).get(kind)


# ── relations ───────────────────────────────────────────────────────────────
def _norm_relations(normen) -> list[TypedRelation]:
    """RIS's ``Normen`` index → ``INTERPRETS`` edges.

    This is an editorially-assigned norm index, not a regex over prose, so the edges are
    ``structured``. It is also where the EU join happens: RIS writes the GDPR as ``DSGVO
    Art15``, which ``citations.austrian`` maps straight onto CELEX 32016R0679.
    """
    out: list[TypedRelation] = []
    for target, anchor, raw in norm_citations(_items(normen)):
        out.append(TypedRelation(
            relationship_type=RelationshipType.INTERPRETS,
            raw_citation_string=raw, dst_id=target, dst_anchor=anchor,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING))
    return out


def _rechtssatz_relations(specific: dict) -> tuple[list[TypedRelation], list[dict]]:
    """A Rechtssatz's application history → typed edges plus the structured record of it.

    One edge per applying decision, typed from ``Anmerkung`` and ``Entscheidungsart``, and
    — where the note names the decision the applying court rejected — a second edge to
    *that* decision. The rejection is an assertion by the applying court, so its docket is
    kept in ``raw_citation_string``: the edge's endpoints are the proposition and the
    rejected case, and the audit trail has to say who did the rejecting.
    """
    edges: list[TypedRelation] = []
    treatments: list[dict] = []
    for entry in _items(specific.get("Entscheidungstexte")):
        if not isinstance(entry, dict):
            continue
        docket = _clean(entry.get("Geschaeftszahl"))
        if not docket:
            continue
        note = _clean(entry.get("Anmerkung"))
        disposition = _clean(entry.get("Entscheidungsart"))
        court = _clean(entry.get("Gericht")) or "OGH"
        kind = RelationshipType.APPLIES
        for pattern, mapped in _TREATMENT:
            if pattern.search(note):
                kind = mapped
                break
        rejected = _ABLEHNUNG_RE.search(note)
        # An express rejection is about the NAMED decision, not about the one that
        # applied the proposition — so the applying decision's own edge stays an
        # application and the overruling edge is emitted separately below.
        edges.append(TypedRelation(
            relationship_type=(RelationshipType.APPLIES if rejected else kind),
            raw_citation_string=f"{docket} ({note})" if note else docket,
            dst_id=case_id(court, docket), dst_anchor=None,
            extracted_via=ExtractedVia.STRUCTURED,
            resolution_status=ResolutionStatus.PENDING))
        if rejected:
            edges.append(TypedRelation(
                relationship_type=RelationshipType.OVERRULES,
                raw_citation_string=f"{docket}: {note}",
                dst_id=case_id("OGH", rejected.group("docket")), dst_anchor=None,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
        treatments.append({k: v for k, v in {
            "docket": docket,
            "court": court,
            "date": _clean(entry.get("Entscheidungsdatum")) or None,
            "disposition": disposition or None,
            "note": note or None,
            "enlarged_panel": True if _ENLARGED_PANEL.search(disposition) else None,
            "rejects": rejected.group("docket") if rejected else None,
            "url": _clean(entry.get("DokumentUrl")) or None,
        }.items() if v not in (None, "")})
    return edges, treatments


def _stammrechtssatz_relations(specific: dict) -> list[TypedRelation]:
    """The VwGH's ``Stammrechtssatznummer`` — this proposition's parent.

    The administrative court restates a settled proposition rather than re-deriving it,
    and records which Rechtssatz it is restating ("GRS wie 2010/03/0165 E 21. Oktober 2011
    RS 5"). That is a derivation between two propositions, and it is the only place the
    chain is published as data rather than as prose.
    """
    parent = _clean(specific.get("Stammrechtssatznummer"))
    if not parent:
        return []
    return [TypedRelation(
        relationship_type=RelationshipType.FOLLOWS,
        raw_citation_string=_clean(specific.get("HinweisAufStammrechtssatz")) or parent,
        dst_id=f"at/ris/{parent}", dst_anchor=None,
        extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.PENDING)]
