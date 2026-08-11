"""Finland — the Finlex open-data API (``fi-*``).

Finlex serves the whole Finnish legal corpus as Akoma Ntoso 3.0 with ELI/ECLI aliases and
a clean incremental primitive. It is the best-modelled source of the five European
case-law APIs added here, and the only one that publishes case law, legislation,
consolidated legislation and preparatory materials through one interface:

```
https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/{tree}/{type}/list   discovery
https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi/doc/{type}/{y}/{n}/{lang}@   retrieval
```

No registration. A ``User-Agent`` header is **required** — omit it and the service
answers 400. ``Accept-Encoding: gzip`` is asked for by the documentation and honoured.

## The routing quirk, which is worse than documented

Listing is under ``/doc/`` and **retrieval is under ``/doc/`` too**. The whole
``/judgment/…`` tree returns 404 — including the exact ``akn_uri`` values the list
endpoint hands back, which point at ``/judgment/``. Following them, as the obvious reading
of the response says to, fetches nothing at all.

Two rewrites are needed, and the second is not guessable:

1. ``/judgment/`` → ``/doc/``.
2. **Drop the court segment.** ``court-of-appeal-decision`` and
   ``administrative-court-decision`` carry one (``/helsinki/2024/1563``), and the
   retrieval route does not accept it — with the court it is 404, without it is 200 and
   returns the Helsinki decision. Since a decision number is only unique within its
   court, the returned ``FRBRWork`` URI is checked against the one asked for, and a
   mismatch is dropped rather than stored under the wrong court's identity.

## Throughput is the real constraint

``limit`` caps at **10** (default 5) on every list route, and the service rate-limits with
429. A full KKO backfill from 1979 is a long, polite crawl, so discovery slices by year
(``startYear``/``endYear``) and every stub carries its year and offset — an interrupted
backfill resumes at a year rather than at the start.

## ``publishedSince`` is a proper incremental key

It returns a per-document ``status`` of ``NEW`` or ``MODIFIED``, which is better than a
bare timestamp: a re-published document is distinguishable from a new one, so a watch can
report what actually changed rather than what merely reappeared.

## Both language versions are separate list entries

``fin@`` and ``swe@`` expressions of one judgment arrive as two rows. They are one Work,
so the Finnish expression is preferred and the Swedish one is recorded as a translation
rather than stored as a second judgment. ``language=swe`` takes the Swedish side instead,
for the Åland and bilingual-municipality material where it is the original.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator
from urllib.parse import urlsplit

from ..core.adapter import BaseAdapter, option_flag, option_int
from ..core.errors import FetchError
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Stub
from ..formats.akoma_ntoso import parse_akn
from ..formats.finlex_akn import finlex_metadata, parse_finlex_akn

BASE = "https://opendata.finlex.fi/finlex/avoindata/v1/akn/fi"
SITE = "https://finlex.fi"

#: ``(tree, document type)`` → ``(label, DocType, court/body)``. The tree is the API's own
#: partition: ``act`` for statutes, ``doc`` for everything else — including judgments,
#: which the *listing* files under ``doc`` even though their identifiers say ``judgment``.
SERIES: dict[str, tuple[str, str, DocType, str | None]] = {
    "fi-kko": ("doc", "supreme-court-precedent", DocType.JUDGMENT, "Korkein oikeus"),
    "fi-kho": ("doc", "supreme-administrative-court-precedent", DocType.JUDGMENT,
               "Korkein hallinto-oikeus"),
    "fi-hovioikeus": ("doc", "court-of-appeal-decision", DocType.JUDGMENT, None),
    "fi-hao": ("doc", "administrative-court-decision", DocType.JUDGMENT, None),
    "fi-mao": ("doc", "market-court-decision", DocType.JUDGMENT, "Markkinaoikeus"),
    "fi-tt": ("doc", "labour-court-decision", DocType.JUDGMENT, "Työtuomioistuin"),
    "fi-vako": ("doc", "insurance-court-decision", DocType.JUDGMENT, "Vakuutusoikeus"),
    "fi-tsv": ("doc", "data-protection-ombudsman-decision", DocType.DECISION,
               "Tietosuojavaltuutetun toimisto"),
    "fi-oka": ("doc", "chancellor-of-justice-decision", DocType.DECISION,
               "Oikeuskanslerinvirasto"),
    "fi-saadokset": ("act", "statute", DocType.LEGISLATION, None),
    "fi-saadokset-ajantasa": ("act", "statute-consolidated", DocType.LEGISLATION, None),
    "fi-he": ("doc", "government-proposal", DocType.PREPARATORY, "Valtioneuvosto"),
    "fi-viranomaismaaraykset": ("doc", "authority-regulation", DocType.GUIDANCE, None),
}

#: The list route's hard ceiling. Documented as "max. 10" in the OpenAPI description and
#: enforced; asking for more is a 400, not a clamp.
_LIMIT = 10
#: KKO's oldest published precedent in the sample is 1979; the statute book reaches 1734.
_EARLIEST_YEAR = {"act": 1734}

#: The court is NOT read from a table here. ``FRBRauthor`` names an organisation by eId
#: and the document's own ``TLCOrganization`` registry states that organisation's display
#: name — "Itä-Suomen hovioikeus" for ``fi.court-of-appeal-eastern-finland``. A hand-kept
#: list of Finnish court names would have to be right about the transliteration of every
#: one of them, and would be wrong about the next court Finlex adds; the document already
#: carries the answer. Only where the stated author is Finlex itself — which it is for the
#: two supreme courts, whose material Finlex edits — does the series' own body name apply.
_EDITOR_AUTHORS = {"fi.finlex"}


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(_clean(value)[:10])
    except ValueError:
        return None


def retrieval_url(akn_uri: str) -> tuple[str, str] | None:
    """An ``akn_uri`` from a list response → ``(fetchable URL, Work path)``, or None.

    Both rewrites the module docstring describes happen here, and the Work path is
    returned alongside so ``fetch`` can verify that what came back is what was asked for.
    """
    path = urlsplit(_clean(akn_uri)).path
    m = re.search(r"/akn/fi/(?P<tree>judgment|doc|act)/(?P<type>[a-z0-9-]+)/(?P<rest>.+)$",
                  path)
    if not m:
        return None
    rest = m.group("rest").rstrip("/")
    parts = [p for p in rest.split("/") if p]
    # {court?}/{year}/{number}/{lang@version}; the court is what retrieval refuses.
    lang = parts[-1] if parts and "@" in parts[-1] else None
    core = parts[:-1] if lang else parts
    if len(core) > 2:
        core = core[-2:]
    tree = "act" if m.group("tree") == "act" else "doc"
    work = "/".join([m.group("type"), *(parts[:-1] if lang else parts)])
    url = f"{BASE}/{tree}/{m.group('type')}/" + "/".join(core)
    if lang:
        url += f"/{lang}"
    return url, work


def work_uri_matches(work_uri: str, expected: str) -> bool:
    """Does a retrieved document's ``FRBRWork`` URI name the document we asked for?

    Dropping the court segment is what makes retrieval work at all, and it is also what
    could silently return Vaasa's decision 1563 when Helsinki's was wanted. The Work URI
    is the publisher's own statement of identity, so comparing it is the check that keeps
    the shortcut honest.
    """
    got = [p for p in _clean(work_uri).split("/") if p]
    want = [p for p in _clean(expected).split("/") if p]
    if not got or not want:
        return True  # nothing to compare against — the caller keeps the document
    return got[-len(want):] == want or want[-len(got):] == got


class FinlexAdapter(BaseAdapter):
    """One Finlex document series. Registered once per series — see ``adapters.registry``."""

    # The service rate-limits with 429 and every page yields at most ten stubs, so the
    # floor is what keeps a multi-decade backfill from being throttled into a stall.
    min_interval = 0.7
    requires_js = False
    requires_proxy = False

    def __init__(
        self,
        *,
        series: str = "fi-kko",
        source_key: str | None = None,
        language: str | None = None,
        keyword: str | None = None,
        title_contains: str | None = None,
        start_year: int | str | None = None,
        end_year: int | str | None = None,
        ids: str | list[str] | None = None,
        include_swedish: bool | str | None = None,
        client: RateLimitedClient | None = None,
    ) -> None:
        if series not in SERIES:
            raise ValueError(
                f"unknown Finlex series {series!r}; known: {', '.join(SERIES)}")
        self.series = series
        self.source = source_key or series
        self.tree, self.doc_type, self.record_type, self.body = SERIES[series]
        self.language = (language or "fin").strip().lower()
        self.keyword = (keyword or "").strip() or None
        self.title_contains = (title_contains or "").strip() or None
        self.start_year = option_int(start_year, 0) or None
        self.end_year = option_int(end_year, 0) or None
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        self.ids = list(ids or [])
        self.include_swedish = option_flag(include_swedish, False)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=90,
            # The service requires a User-Agent and answers 400 without one.
            user_agent="raglex/1.0 (legal research corpus)")

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            yield from self._discover_ids()
            return
        if since:
            # ``publishedSince`` is the service's own incremental key and returns a
            # NEW/MODIFIED status per document, so a watch needs no year slicing at all.
            yield from self._walk({"publishedSince": f"{_clean(since)[:10]}T00:00:00Z"},
                                  max_pages=max_pages)
            return
        first = self.start_year or _EARLIEST_YEAR.get(self.tree) or 1979
        last = self.end_year or date.today().year
        emitted = 0
        for year in range(last, first - 1, -1):
            for stub in self._walk({"startYear": year, "endYear": year}, max_pages=None,
                                   year=year):
                yield stub
                emitted += 1
            if max_pages is not None and emitted >= max_pages * _LIMIT:
                return

    def _walk(self, params: dict, *, max_pages: int | None,
              year: int | None = None) -> Iterator[Stub]:
        base = {"limit": _LIMIT, "sortBy": "dateIssued"}
        if self.keyword:
            base["keyword"] = self.keyword
        if self.title_contains:
            base["titleContains"] = self.title_contains
        page = 1
        seen: set[str] = set()
        offset = 0
        while True:
            rows = self._list({**base, **params, "page": page})
            if not rows:
                return
            for row in rows:
                stub = self._stub(row, year=year, offset=offset)
                offset += 1
                if stub is None or stub.stable_id in seen:
                    continue
                seen.add(stub.stable_id)
                yield stub
            if len(rows) < _LIMIT:
                return
            page += 1
            if max_pages is not None and page > max_pages:
                return

    def _discover_ids(self) -> Iterator[Stub]:
        """A targeted pull by ECLI, diary number, or ``{year}/{number}``."""
        for ident in self.ids:
            ident = ident.strip()
            if not ident:
                continue
            if m := re.fullmatch(r"(\d{4})[/:](\S+)", ident):
                akn = (f"{BASE}/{self.tree}/{self.doc_type}/{m.group(1)}/{m.group(2)}"
                       f"/{self.language}@")
                stub = self._stub({"akn_uri": akn, "status": "NEW"})
                if stub is not None:
                    yield stub
                continue
            key = "ecli" if ident.upper().startswith("ECLI:") else "diaryNumber"
            for row in self._list({key: ident, "limit": _LIMIT}):
                stub = self._stub(row)
                if stub is not None:
                    yield stub

    def _stub(self, row: dict, *, year: int | None = None,
              offset: int | None = None) -> Stub | None:
        akn = _clean((row or {}).get("akn_uri"))
        if not akn:
            return None
        # Both language expressions of one Work arrive as separate rows. Keep the one
        # this run is for; the other is the same judgment, not a second one.
        expression = akn.rstrip("/").rsplit("/", 1)[-1]
        wanted = self.language
        if "@" in expression:
            lang = expression.split("@", 1)[0].lower()
            if lang != wanted and not (self.include_swedish and lang == "swe"):
                return None
        resolved = retrieval_url(akn)
        if resolved is None:
            return None
        url, work = resolved
        hints = {"akn_uri": akn, "fetch_url": url, "work": work,
                 "status": _clean((row or {}).get("status")) or None}
        if year is not None:
            hints["year"] = year
        if offset is not None:
            hints["resume_offset"] = offset
        return Stub(
            stable_id=f"fi/{work}",
            landing_url=akn,
            title=work,
            court=self.body,
            hints=hints,
        )

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        raw = self._bytes(stub.hints.get("fetch_url") or "")
        if not raw:
            return None
        if self.record_type == DocType.LEGISLATION:
            # An act's BODY is ordinary Akoma Ntoso legislation, which the shared parser
            # already segments by luku/pykälä; only its meta block is Finlex-specific.
            parsed = parse_akn(raw)
            meta = {**finlex_metadata(raw), **(parsed.metadata or {})}
        else:
            parsed = parse_finlex_akn(raw)
            meta = parsed.metadata or {}
        work_uri = _clean(meta.get("work_uri"))
        expected = _clean(stub.hints.get("work"))
        if work_uri and expected and not work_uri_matches(work_uri, expected):
            # The court segment had to be dropped to fetch anything at all; this is where
            # that shortcut is checked. A document whose own identity disagrees with the
            # one requested is not stored under it.
            return None
        if not (parsed.text or "").strip():
            # A metadata-only AKN is a wrapper over main.pdf — see ``formats.finlex_akn``.
            pdf = (stub.hints.get("fetch_url") or "").rstrip("/") + "/main.pdf"
            body = self._bytes(pdf)
            if body:
                from ..extraction import ocr as _ocr

                text, needs_ocr, _spans, _engine = _ocr.text_or_ocr(body)
                if (text or "").strip():
                    return self._record(stub, parsed, raw=body, ext="pdf", text=text,
                                        meta=meta, needs_ocr=needs_ocr, pdf_url=pdf)
            return None
        return self._record(stub, parsed, raw=raw, ext="xml", text=parsed.text, meta=meta)

    def _record(self, stub: Stub, parsed, *, raw: bytes, ext: str, text: str,
                meta: dict, needs_ocr: bool = False, pdf_url: str | None = None) -> Record:
        dates = meta.get("dates") or {}
        decided = (_iso(dates.get("dateIssued")) or _iso(dates.get("datePublished"))
                   or _iso(dates.get("dateIssuedGenerated")))
        ecli = _clean(meta.get("ecli"))
        author = _clean(meta.get("author"))
        court = (self.body if author in _EDITOR_AUTHORS or not author
                 else _clean(meta.get("author_name")) or self.body)
        # The citable name, in the order a Finnish lawyer would say it: "KKO:2024:1"
        # where the court publishes one, else the court, its case number and its date.
        title = (_clean(meta.get("doc_number")) or _clean(parsed.title)
                 or _clean(meta.get("doc_title")) or stub.stable_id)
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            ecli=ecli if ecli.upper().startswith("ECLI:") else None,
            doc_type=self.record_type,
            title=title,
            court=court,
            decision_date=decided,
            language=(_clean(meta.get("language")) or self.language)[:3],
            source_language=(_clean(meta.get("language")) or self.language)[:3],
            landing_url=_finlex_link(stub.hints.get("work") or "", ecli),
            raw_bytes=raw,
            raw_ext=ext,
            text=text,
            segments=parsed.segments,
            extracted_via=ExtractedVia.STRUCTURED,
            topic_tags=[t for t in (meta.get("keywords") or []) if t],
            extra={k: v for k, v in {
                "jurisdiction": "fi",
                "series": self.series,
                "document_type": self.doc_type,
                "akn_uri": _clean(stub.hints.get("akn_uri")) or None,
                "work_uri": _clean(meta.get("work_uri")) or None,
                "diary_number": _clean(meta.get("diary_number")) or None,
                "case_number": _clean(meta.get("case_number")) or None,
                "decision_number": _clean(meta.get("decision_number")) or None,
                "archival_record": _clean(meta.get("archival_record")) or None,
                # The Data Protection Ombudsman's decisions state which instrument they
                # were taken under, as an ontology concept. That is the difference between
                # a GDPR decision and a national-law one, and it is machine-stated.
                "legal_basis": meta.get("legal_basis") or None,
                "concepts": meta.get("concepts") or None,
                "zones": meta.get("zones") or None,
                "produced_at": (meta.get("dates") or {}).get("dateProduced"),
                "published_at": (meta.get("dates") or {}).get("datePublished"),
                "eli": _clean(meta.get("eli")) or None,
                "feed_status": _clean(stub.hints.get("status")) or None,
                "pdf_url": pdf_url,
                "needs_ocr": needs_ocr or None,
                "aliases": _aliases(meta, stub) or None,
            }.items() if v not in (None, "", [], {})},
        )

    # -- http ----------------------------------------------------------------
    def _list(self, params: dict) -> list[dict]:
        url = f"{BASE}/{self.tree}/{self.doc_type}/list"
        try:
            resp = self._client.get(url, params=params,
                                    headers={"Accept": "application/json",
                                             "Accept-Encoding": "gzip"},
                                    raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return []
        if resp.status_code >= 400:
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []
        return [row for row in (payload or []) if isinstance(row, dict)]

    def _bytes(self, url: str) -> bytes | None:
        if not url:
            return None
        try:
            resp = self._client.get(url, headers={"Accept-Encoding": "gzip"},
                                    raise_for_4xx=False)
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        return resp.content if resp.status_code < 400 and resp.content else None


def _aliases(meta: dict, stub: Stub) -> list[str]:
    """The forms Finnish practice cites this document by.

    ``KKO:2024:1`` and ``HelHO:2024:12`` are what a judgment writes, and the citation
    grammar mints ``fi/kko/2024/1`` from them — so that is the alias, not the AKN path.
    """
    out: list[str] = []
    for value in (_clean(meta.get("doc_number")), _clean(meta.get("case_number"))):
        if m := re.fullmatch(r"([A-Za-zÅÄÖåäö]{2,8})\s*:\s*(\d{4})\s*:\s*(\S+)", value):
            out.append(f"fi/{m.group(1).casefold()}/{int(m.group(2))}/"
                       f"{m.group(3).strip().lower()}")
    if diary := _clean(meta.get("diary_number")):
        out.append(f"fi:diary:{diary}")
    return [a for a in dict.fromkeys(out) if a]


def _finlex_link(work: str, ecli: str) -> str | None:
    """The page a reader follows to the original on finlex.fi."""
    if ecli:
        return f"{SITE}/fi/oikeuskaytanto/{ecli}"
    return f"{SITE}/fi/lainsaadanto/{work}" if work else None
