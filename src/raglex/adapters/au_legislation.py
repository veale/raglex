"""Australian legislation adapters — the Commonwealth OData API + the LawMaker states.

**"Australian legislation" is nine separate jurisdictions**, each with its own register,
platform, identifiers and licence; there is no national API. So the design is *one
normalised model, many adapters*, and every record carries a first-class ``jurisdiction``
key (``au/{juris}/…``). This module implements the two highest-value, cleanest tiers —
the ones the manual says to build first, covering the Commonwealth plus the three
largest open states:

* **``au-cth``** (Commonwealth) — the crown jewel: a keyless **OData v4 REST API** at
  ``api.prod.legislation.gov.au``. You *query* rather than crawl. It hands over the
  amendment graph as structured edges (``statusHistory``), the point-in-time version
  series (``Versions/Find``), the originating Bill link, and the name history — all
  inline. The document *body* is a **separate binary fetch**: metadata and content are
  different endpoint families on this API, and ``documents/find`` returns the compilation
  as an EPUB/Word/PDF octet-stream, never as text inside the JSON (see
  :meth:`CommonwealthAdapter._fetch_body`).
* **``au-qld`` / ``au-nsw`` / ``au-tas``** (LawMaker states) — one adapter, three
  jurisdictions, because they share the Lawlab/LawMaker platform with an identical,
  deterministic, point-in-time-addressable URL grammar
  (``/view/whole/html/{status}/{date}/{docid}``). Discovery is the crawler feed
  (Qld/Tas) or explicit ids; point-in-time is just a path segment.

Everything maps onto the same Work → Expression → Format + temporal tiers as the Irish
adapters ([[irish-legislation]]), so Australia slots in beside Ireland/UK/EU rather than
being a bespoke silo. All nine registers are the *authorised* source for their own
jurisdiction (unlike Ireland's non-authoritative LRC consolidations), so ``is_authoritative``
is True — but the FRL's ``hasUnincorporatedAmendments`` flag (amendments in force but not
yet folded into the compilation) is surfaced exactly like Ireland's authoritativeness flag.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
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
    Stub,
    TypedRelation,
)
from ..formats import parse
from ..formats.lawmaker_html import au_id, parse_lawmaker_html

# re-exported so callers/tests have one obvious home for the id helper
__all__ = ["au_id", "CommonwealthAdapter", "LawMakerAdapter"]

FRL_API = "https://api.prod.legislation.gov.au/v1"
FRL_SITE = "https://www.legislation.gov.au"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# FRL register-id grammar: C{year}A{nnnnn} = Act, F{year}{L|N}{nnnnn} = instrument,
# C{year}C{nnnnn} = a compilation (a point-in-time version, its OWN id). One Work (Title)
# has one A/F id and many C…C… compilation ids over time.
_FRL_ID_RE = re.compile(r"^(?P<c>[CF])(?P<year>\d{4})(?P<series>[A-Z])(?P<num>\d{5})$")
_FRL_SERIES_TYPE = {"A": "act", "L": "sl", "N": "ni", "C": "compilation"}

# The register's own collections → our doc-type-ish subtype. Acts + instruments are the
# legislative mass; the rest (gazettes, arrangements) are out of the default scope.
DEFAULT_COLLECTION = "Act"


def frl_stable_id(title_id: str) -> str:
    """An FRL Title id → the corpus stable_id: ``C1901A00002`` → ``au/cth/act/1901/2``.
    Falls back to carrying the raw register id (prefixed) for id shapes we don't model,
    so nothing is ever silently dropped."""
    m = _FRL_ID_RE.match(title_id or "")
    if not m:
        return f"au/cth/{(title_id or '').lower()}"
    series = _FRL_SERIES_TYPE.get(m.group("series"), m.group("series").lower())
    return au_id("cth", series, int(m.group("year")), str(int(m.group("num"))))


def _iso(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# -- Commonwealth: the OData API --------------------------------------------
@dataclass(frozen=True, slots=True)
class Amendment:
    """One structured amendment edge from ``statusHistory``/``Version.reasons`` — the
    FRL hands these over machine-readable, so no note-parsing is needed (unlike the
    states, where the same data lives in endnote tables)."""
    affect: str                 # "Amend" | "Repeal" | …
    affected_by_id: str | None  # the amending Title's register id
    provisions: str | None
    citation: str | None        # the human "markdown" citation


def parse_reasons(reasons: list[dict]) -> list[Amendment]:
    out: list[Amendment] = []
    for r in reasons or []:
        by = r.get("affectedByTitle") or {}
        out.append(Amendment(
            affect=r.get("affect") or "",
            affected_by_id=by.get("titleId"),
            provisions=by.get("provisions"),
            citation=(r.get("markdown") or "").strip() or None,
        ))
    return out


class CommonwealthAdapter(BaseAdapter):
    """Federal Register of Legislation — Commonwealth (Cth). OData API."""

    source = "au-cth"
    min_interval = 1.0   # robots Crawl-delay: 10 is a courtesy floor; self-limit anyway
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 collection: str | None = None, filter: str | None = None,
                 principal_only: bool = True, page_size: int = 100,
                 client: RateLimitedClient | None = None) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        self.collection = (collection or DEFAULT_COLLECTION).strip()
        self.extra_filter = (filter or "").strip() or None
        self.principal_only = str(principal_only).lower() not in ("false", "0", "no")
        # The FRL API caps $top at 100 — a larger page 400s ("The limit of '100' for Top
        # query has been exceeded"), which silently returned zero titles.
        self.page_size = max(1, min(int(page_size), 100))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, user_agent=_UA, timeout=60)

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.ids:
            for raw in self.ids:
                tid = _title_id(raw)
                yield Stub(stable_id=frl_stable_id(tid),
                           landing_url=f"{FRL_SITE}/{tid}",
                           hints={"title_id": tid})
            return
        yield from self._discover_query(since, max_pages=max_pages)

    def _discover_query(self, since: str | None, *, max_pages: int | None) -> Iterator[Stub]:
        """Page the ``Titles`` set with ``$filter``/``$orderby``. Incremental by
        ``asMadeRegisteredAt`` (the register-published timestamp) — a daily run pulls
        everything registered since the cursor, newest first, and stops at it.

        ``isPrincipal`` is **post-filtered client-side**, not sent in ``$filter``: the FRL
        API returns the field but no longer accepts it as a filter predicate (a
        ``$filter`` containing ``isPrincipal eq true`` now 400s, which silently returned
        zero titles). So we select the field and drop non-principal rows after the fetch.
        The same applies to range predicates on ``year`` (``year ge 1990`` 400s; only
        equality is accepted), which is why a year scope is expressed as ``year eq YYYY``.

        A **backfill walks newest-first** (``year desc``). Ordering by ``id`` — the obvious
        choice, and what this did — is effectively oldest-first, which meant a full-catalogue
        run spent its first days on 1901-1920s Acts. Those are the worst possible ones to
        start with: the register only generates the unzipped-EPUB HTML this adapter reads
        for *recent* compilations, so an Act last compiled in 1901 (or 1996) yields metadata
        and no text at all, and they are the least-cited material in the corpus. Newest-first
        gets the Acts that actually have text and that judgments actually cite."""
        clauses = [f"collection eq '{self.collection}'"]
        if self.extra_filter:
            clauses.append(f"({self.extra_filter})")
        if since:
            clauses.append(f"asMadeRegisteredAt gt {since}")
        filt = " and ".join(clauses)

        pages = 0
        skip = 0
        while True:
            params = {
                "$filter": filt,
                "$orderby": "asMadeRegisteredAt desc" if since else "year desc",
                "$top": self.page_size,
                "$skip": skip,
                "$select": "id,name,collection,year,number,status,isInForce,isPrincipal,"
                           "asMadeRegisteredAt,makingDate",
                "$format": "json",
            }
            try:
                resp = self._client.get(f"{FRL_API}/titles", params=params)
                rows = json.loads(resp.content).get("value", [])
            except (FetchError, json.JSONDecodeError):
                return
            if not rows:
                return
            for row in rows:
                tid = row.get("id")
                if not tid:
                    continue
                # Principal-title filter, applied client-side (see docstring).
                if self.principal_only and row.get("isPrincipal") is False:
                    continue
                yield Stub(
                    stable_id=frl_stable_id(tid),
                    landing_url=f"{FRL_SITE}/{tid}",
                    title=row.get("name"),
                    hint_date=_iso(row.get("asMadeRegisteredAt")),
                    hints={"title_id": tid,
                           "watermark": row.get("asMadeRegisteredAt")},
                )
            pages += 1
            skip += len(rows)
            if len(rows) < self.page_size or (max_pages is not None and pages >= max_pages):
                return

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        tid = stub.hints["title_id"]
        try:
            title = json.loads(self._client.get(
                f"{FRL_API}/titles('{tid}')", params={"$format": "json"}).content)
        except (FetchError, json.JSONDecodeError):
            return None
        if not title.get("id"):
            return None

        version = self._current_version(tid)
        relations: list[TypedRelation] = []

        # The amendment graph, structured — no endnote parsing. statusHistory reasons +
        # the current compilation's reasons both name the amending Title by register id.
        amendments: list[Amendment] = []
        for entry in title.get("statusHistory", []):
            amendments.extend(parse_reasons(entry.get("reasons", [])))
        if version:
            amendments.extend(parse_reasons(version.get("reasons", [])))
        seen: set[tuple] = set()
        for a in amendments:
            if not a.affected_by_id:
                continue
            dst = frl_stable_id(a.affected_by_id)
            key = (dst, a.provisions, a.affect)
            if dst == stub.stable_id or key in seen:
                continue
            seen.add(key)
            relations.append(TypedRelation(
                relationship_type=RelationshipType.AMENDED_BY,
                raw_citation_string=a.citation or a.affected_by_id, dst_id=dst,
                src_anchor=a.provisions, dst_anchor=a.affect,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))

        # The originating Bill — the statute → parliamentary-history edge (mirrors
        # Ireland's related_to Bill link); kept as a mention so it isn't lost.
        bill = title.get("originatingBillUri")

        # Body text comes from the API's *content* endpoint (a binary EPUB), not from the
        # metadata JSON and not from the website. The two scrape paths below are retained
        # only as a safety net; the API route is the one that works for repealed, old and
        # uncompiled titles alike.
        body_pit = (version or {}).get("start", "")[:10] or None
        text_doc, as_at_used = self.fetch_body_api(tid)
        if text_doc is None:
            text_doc = self._fetch_body(tid, body_pit, body_pit)
            if text_doc is None:
                text_doc, body_pit = self._fetch_body_fallback(tid)
        elif as_at_used != "Current":
            # we hold an older or as-made compilation, not the in-force one — don't let the
            # current version's date stand in for text we didn't get from it
            body_pit = None

        title_name = title.get("name") or (text_doc.title if text_doc else None) or stub.stable_id
        segments = text_doc.segments if text_doc else []
        text = text_doc.text if text_doc else None
        relations.extend(text_doc.relations if text_doc else [])

        status = title.get("status")
        unincorporated = bool(title.get("hasCommencedUnincorporatedAmendments")
                              or (version or {}).get("hasUnincorporatedAmendments"))
        extra = {
            "jurisdiction": "cth",
            "frl_title_id": tid,
            "collection": title.get("collection"),
            "series_type": title.get("seriesType"),
            "year": title.get("year"),
            "number": title.get("number"),
            "status": status,
            "is_in_force": title.get("isInForce"),
            "is_principal": title.get("isPrincipal"),
            "is_authoritative": True,   # the FRL is the authorised Cth source
            # amendments in force but not yet folded into the compilation → the API text
            # is not fully current; surface it, don't present stale text as in-force.
            "has_unincorporated_amendments": unincorporated,
            "originating_bill_uri": bill,
            "making_date": (title.get("makingDate") or "")[:10] or None,
            "as_made_registered_at": (title.get("asMadeRegisteredAt") or "")[:10] or None,
            "name_history": [h.get("name") for h in title.get("nameHistory", []) if h.get("name")],
        }
        if version:
            extra.update({
                "compilation_register_id": version.get("registerId"),
                "compilation_number": version.get("compilationNumber"),
                # the date whose text we actually hold — the current compilation's when
                # its HTML exists, else the fallback compilation's
                "point_in_time": body_pit,
                "format": text_doc.metadata.get("format") if text_doc else None,
            })
        if text_doc and text_doc.metadata.get("endnotes"):
            extra["endnotes"] = text_doc.metadata["endnotes"]

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=title_name,
            language="en", source_language="en",
            decision_date=_iso(title.get("makingDate")),
            landing_url=stub.landing_url,
            raw_bytes=json.dumps(title).encode(), raw_ext="json",
            text=text, segments=segments, relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v is not None},
        )

    def _current_version(self, title_id: str) -> dict | None:
        try:
            data = json.loads(self._client.get(
                f"{FRL_API}/Versions/Find(titleId='{title_id}',asAtSpecification='Current')",
                params={"$format": "json"}).content)
        except (FetchError, json.JSONDecodeError):
            return None
        return data if data.get("registerId") else None

    # ``asAtSpecification`` values to try, in order. "Current" is the in-force compilation
    # and is what we want when it exists; it 404s for anything repealed or not currently
    # compiled, where "Latest" (the most recent compilation there is) succeeds. "AsMade"
    # is the original enactment — the last resort, and the only text some very old Acts
    # have. Trying all three is what takes body coverage from a handful of titles to
    # effectively all of them.
    _AS_AT_ORDER = ("Current", "Latest", "AsMade")

    def _content_url(self, title_id: str, as_at: str, fmt: str = "Epub") -> str:
        """The register's **content** endpoint. Note this is a different endpoint family
        from the metadata sets: it streams the compilation as an octet-stream, and OData
        will not resolve the function unless *every* parameter in the signature is
        supplied — omitting ``uniqueTypeNumber``/``volumeNumber``/``rectificationSpecification``
        yields a 404 that reads exactly like "this document doesn't exist"."""
        return (f"{FRL_API}/documents/find(titleid='{title_id}',"
                f"asatspecification='{as_at}',type='Primary',format='{fmt}',"
                f"uniqueTypeNumber=0,volumeNumber=0,rectificationSpecification='Latest')")

    def _fetch_body_fallback(self, title_id: str):
        """Deprecated site-scrape path, kept only as a last resort behind the API fetch.

        It walks ``legislation.gov.au/{id}/{date}/{date}/text/{vol}/epub/OEBPS/…`` — an
        *incidental* static path that exists for some compilations and not others, which is
        why it produced text for 19 of 1,204 titles. :meth:`_fetch_body` (the documented
        content endpoint) supersedes it."""
        try:
            data = json.loads(self._client.get(
                f"{FRL_API}/Documents",
                params={"$filter": f"titleId eq '{title_id}' and format eq 'Epub'",
                        "$select": "start,retrospectiveStart", "$format": "json"}).content)
        except (FetchError, json.JSONDecodeError):
            return None, None
        rows = sorted(data.get("value", []), key=lambda r: r.get("start") or "", reverse=True)
        for row in rows[:6]:  # a handful of most-recent compilations is plenty
            start = (row.get("start") or "")[:10]
            retro = (row.get("retrospectiveStart") or row.get("start") or "")[:10]
            doc = self._fetch_body(title_id, start, retro)
            if doc is not None:
                return doc, start
        return None, None

    def fetch_body_api(self, title_id: str):
        """A title's text via the register's documented content endpoint. Returns
        ``(ParsedDoc | None, as_at_specification_used | None)``.

        The FRL API keeps **metadata and content in separate endpoint families**: the
        ``Titles``/``Versions`` sets return JSON that never contains the law's text, and
        ``documents/find`` returns the compilation itself as an octet-stream. Only four
        formats exist — ``Word``, ``Pdf``, ``Epub``, ``NameOnly`` — with no text/HTML/XML
        option, so EPUB is the parse-friendly choice: it is a zip of XHTML, one member per
        volume, which the existing ``frl-html`` parser reads unchanged.

        Each ``asAtSpecification`` is tried in turn (see :attr:`_AS_AT_ORDER`), because a
        repealed or uncompiled Act has no "Current" document but does have a "Latest" or
        "AsMade" one."""
        import io
        import zipfile

        from ..formats.base import ParsedDoc

        for as_at in self._AS_AT_ORDER:
            try:
                resp = self._client.get(self._content_url(title_id, as_at))
            except FetchError:
                continue
            blob = resp.content or b""
            if blob[:2] != b"PK":          # not a zip → not an EPUB we can read
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                    members = sorted(n for n in zf.namelist()
                                     if n.lower().endswith((".html", ".xhtml")))
                    docs = [parse("frl-html", zf.read(n)) for n in members]
            except (zipfile.BadZipFile, KeyError):
                continue
            docs = [d for d in docs if d.text]
            if not docs:
                continue
            merged = ParsedDoc(text="", segments=[], relations=[], metadata={})
            parts: list[str] = []
            cursor = 0
            for doc in docs:                       # multi-volume Acts: one member each
                if parts:
                    cursor += 2                    # the "\n\n" join below
                for seg in doc.segments:
                    merged.segments.append(type(seg)(
                        label=seg.label, char_start=cursor + seg.char_start,
                        char_end=cursor + seg.char_end, kind=seg.kind, level=seg.level))
                parts.append(doc.text)
                cursor += len(doc.text)
                if merged.title is None:
                    merged.title = doc.title
                merged.metadata.setdefault("endnotes", []).extend(
                    doc.metadata.get("endnotes") or [])
                merged.relations.extend(doc.relations)
            merged.text = "\n\n".join(parts)
            merged.metadata["format"] = "frl-epub"
            merged.metadata["as_at_specification"] = as_at
            return merged, as_at
        return None, None

    def _fetch_body(self, title_id: str, start: str | None, retro: str | None):
        """Legacy site-scrape of the unzipped-EPUB HTML, kept behind the API fetch.

        The URL is ``{titleId}/{start}/{retrospectiveStart}/text/{vol}/epub/OEBPS/
        document_{vol}/document_{vol}.html`` — the two dates are the Version's two time
        axes (legal-effect vs retrospective knowledge). Multi-volume Acts split into
        ``document_1…N``; volumes are walked until one 404s. That static tree only exists
        for some compilations, which is why this is no longer the primary route."""
        if not start:
            return None
        retro = retro or start
        from ..formats.base import ParsedDoc

        merged = ParsedDoc(text="", segments=[], relations=[], metadata={})
        parts: list[str] = []
        cursor = 0
        for vol in range(1, 21):
            url = (f"{FRL_SITE}/{title_id}/{start}/{retro}/text/{vol}"
                   f"/epub/OEBPS/document_{vol}/document_{vol}.html")
            try:
                resp = self._client.get(url)
            except FetchError:
                break
            doc = parse("frl-html", resp.content or b"")
            if not doc.text:
                break
            if parts:
                cursor += 2  # SEP
            for seg in doc.segments:
                merged.segments.append(type(seg)(
                    label=seg.label, char_start=cursor + seg.char_start,
                    char_end=cursor + seg.char_end, kind=seg.kind, level=seg.level))
            parts.append(doc.text)
            cursor += len(doc.text)
            if merged.title is None:
                merged.title = doc.title
            merged.metadata.setdefault("endnotes", []).extend(doc.metadata.get("endnotes") or [])
        if not parts:
            return None
        merged.text = "\n\n".join(parts)
        merged.metadata["format"] = "frl-html"
        return merged


# -- LawMaker states: Qld / NSW / Tas ---------------------------------------
@dataclass(frozen=True, slots=True)
class FeedItem:
    docid: str
    title: str
    url: str
    status: str          # inforce | asmade | repealed
    pit_date: str | None  # the point-in-time date baked into the URL
    published: str | None
    repealed: bool = False


_VIEW_RE = re.compile(
    r"/view/whole/(?P<fmt>html|pdf)/(?P<status>inforce|asmade|repealed)/"
    r"(?P<date>\d{4}-\d{2}-\d{2})/(?P<docid>[a-z]+-\d{4}-\d+[a-z]?)", re.I)
_DOCID_RE = re.compile(r"^(?P<type>act|sl|sr|si)-(?P<year>\d{4})-(?P<num>\d+[a-z]?)$", re.I)
_ATOM = "{http://www.w3.org/2005/Atom}"

LAWMAKER_HOSTS = {
    "qld": "https://www.legislation.qld.gov.au",
    "nsw": "https://legislation.nsw.gov.au",
    "tas": "https://www.legislation.tas.gov.au",
}
_LAWMAKER_TYPES = {"act": "act", "sl": "sl", "sr": "sr", "si": "si"}


def parse_crawler_feed(xml_bytes: bytes) -> list[FeedItem]:
    """Parse a Qld/Tas crawler feed (Atom). Each entry is a recently new/updated title
    with its exact ``/view/whole/html/{status}/{date}/{docid}`` URL — a ready-made
    change-detection delta feed."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    items: list[FeedItem] = []
    for entry in root.findall(f"{_ATOM}entry"):
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        link = entry.find(f"{_ATOM}link")
        href = (link.get("href") if link is not None else None) or entry.findtext(f"{_ATOM}id")
        m = _VIEW_RE.search(href or "")
        if not m:
            continue
        items.append(FeedItem(
            docid=m.group("docid").lower(),
            title=re.sub(r"\s*\(Repealed\)\s*$", "", title, flags=re.I),
            url=(href or "").replace(":443", ""),
            status=m.group("status").lower(),
            pit_date=m.group("date"),
            published=(entry.findtext(f"{_ATOM}updated") or "").strip() or None,
            repealed="(repealed)" in title.lower(),
        ))
    return items


def lawmaker_stable_id(jurisdiction: str, docid: str) -> str | None:
    m = _DOCID_RE.match(docid or "")
    if not m:
        return None
    return au_id(jurisdiction, _LAWMAKER_TYPES[m.group("type").lower()],
                 m.group("year"), m.group("num"))


class LawMakerAdapter(BaseAdapter):
    """Qld / NSW / Tas — the shared Lawlab/LawMaker platform.

    Deterministic, point-in-time-addressable URLs mean one adapter serves all three; the
    jurisdiction is fixed at construction (``au-qld``/``au-nsw``/``au-tas`` in the
    registry). Three discovery routes (use the one that fits):

    * **explicit ids** — fetch exactly those docids;
    * **crawler feed** (Qld/Tas, the default) — recently new/updated titles, i.e. the
      incremental "keep current" delta feed;
    * **id enumeration** (``enumerate=true`` or ``years=…``) — the full-catalogue
      backfill. The registers publish no complete sitemap (Qld's has ~60 URLs) and the
      feed is deltas only, so completeness comes from walking ``{type}-{year}-{n}``
      against the deterministic ``inforce`` endpoint, per year, stopping after a run of
      consecutive misses (the manual's route 3). NSW, which has no headless-reachable
      feed, relies on this for anything beyond named ids.
    """

    requires_js = False
    requires_proxy = False
    min_interval = 1.0

    # Per-jurisdiction document types and their zero-pad width — LawMaker's number width
    # is not uniform (Qld Acts are 3-digit ``act-2016-001``, subordinate legislation
    # 4-digit ``sl-2023-0107``). Overridable via ``types=act:3,sl:4``.
    DEFAULT_TYPES = {
        "qld": {"act": 3, "sl": 4},
        "nsw": {"act": 3, "sl": 4, "epi": 4},
        "tas": {"act": 3, "sr": 3},
    }
    FIRST_YEAR = 1839  # oldest colonial acts still on the registers; tune via years=

    def __init__(self, *, jurisdiction: str, ids: str | tuple[str, ...] | None = None,
                 status: str = "inforce", years: str | None = None,
                 types: str | None = None, enumerate: bool | str = False,
                 miss_streak: int = 8, client: RateLimitedClient | None = None) -> None:
        self.jurisdiction = jurisdiction.lower()
        if self.jurisdiction not in LAWMAKER_HOSTS:
            raise ValueError(f"unknown LawMaker jurisdiction {jurisdiction!r}")
        self.source = f"au-{self.jurisdiction}"
        self.host = LAWMAKER_HOSTS[self.jurisdiction]
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        self.status = (status or "inforce").lower()
        self.years = _year_span(years)
        self.types = _type_widths(types) or dict(self.DEFAULT_TYPES[self.jurisdiction])
        # enumeration (full-catalogue backfill) is opt-in: it's request-heavy against a
        # small government service, so the default stays the cheap feed.
        self.enumerate = str(enumerate).lower() not in ("false", "0", "no", "") or bool(years)
        self.miss_streak = max(3, int(miss_streak))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, user_agent=_UA, timeout=60)

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.enumerate and not self.ids:
            yield from self._discover_enumerate(max_pages=max_pages)
            return
        if self.ids:
            for raw in self.ids:
                docid = _norm_docid(raw)
                sid = lawmaker_stable_id(self.jurisdiction, docid) if docid else None
                if not sid:
                    continue
                today = date.today().isoformat()
                yield self._stub(docid, sid, self.status, today, title=None)
            return
        # Feed-driven (Qld/Tas). NSW has no feed we can fetch headless → id-only.
        try:
            resp = self._client.get(f"{self.host}/feed", params={"id": "crawler"})
        except FetchError:
            return
        for item in parse_crawler_feed(resp.content):
            sid = lawmaker_stable_id(self.jurisdiction, item.docid)
            if not sid:
                continue
            if since and item.published and item.published <= since:
                continue
            yield self._stub(item.docid, sid, item.status, item.pit_date or date.today().isoformat(),
                             title=item.title, repealed=item.repealed,
                             published=item.published)

    def _discover_enumerate(self, *, max_pages: int | None) -> Iterator[Stub]:
        """Full-catalogue backfill: for each (type, year) walk ``{type}-{year}-{n}`` from
        n=1, probing the ``inforce`` endpoint, and stop that year once ``miss_streak``
        consecutive numbers 404. A hit yields a stub (whose fetch re-reads the same URL);
        misses cost one cheap request each. ``max_pages`` (if set) caps the number of
        (type, year) buckets so a UI-triggered run stays bounded."""
        today = date.today().isoformat()
        current = date.today().year
        start, end = self.years or (self.FIRST_YEAR, current)
        buckets = 0
        for typ, width in self.types.items():
            for year in range(end, start - 1, -1):  # newest years first
                misses = 0
                n = 0
                while misses < self.miss_streak:
                    n += 1
                    docid = f"{typ}-{year}-{n:0{width}d}"
                    url = f"{self.host}/view/whole/html/{self.status}/{today}/{docid}"
                    try:
                        resp = self._client.get(url)
                        ok = bool(resp.content) and b"<div id=\"fragview\"" in resp.content
                    except FetchError:
                        ok = False
                    if not ok:
                        misses += 1
                        continue
                    misses = 0
                    sid = lawmaker_stable_id(self.jurisdiction, docid)
                    if sid:
                        yield self._stub(docid, sid, self.status, today, title=None)
                buckets += 1
                if max_pages is not None and buckets >= max_pages:
                    return

    def _stub(self, docid: str, sid: str, status: str, pit: str, *, title: str | None,
              repealed: bool = False, published: str | None = None) -> Stub:
        url = f"{self.host}/view/whole/html/{status}/{pit}/{docid}"
        return Stub(
            stable_id=sid, title=title, landing_url=url, raw_url=url,
            hint_date=_iso(published),
            hints={"docid": docid, "status": status, "pit": pit,
                   "repealed": repealed,
                   "watermark": published} if published else
                  {"docid": docid, "status": status, "pit": pit, "repealed": repealed},
        )

    def fetch(self, stub: Stub) -> Record | None:
        try:
            resp = self._client.get(stub.raw_url)
        except FetchError:
            return None
        body = resp.content or b""
        if not body:
            return None
        parsed = parse_lawmaker_html(body, jurisdiction=self.jurisdiction)
        if not parsed.text:
            return None
        relations = [r for r in parsed.relations if r.dst_id != stub.stable_id]
        title = stub.title or parsed.title or stub.stable_id
        h = stub.hints
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=title,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=body, raw_ext="html",
            text=parsed.text, segments=parsed.segments, relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={
                "jurisdiction": self.jurisdiction,
                "format": "lawmaker-html",
                "docid": h["docid"],
                "long_title": parsed.metadata.get("long_title"),
                "is_authoritative": True,   # each register is the authorised source
                "text_status": h["status"],
                "point_in_time": h["pit"] if h["status"] == "inforce" else None,
                "repealed": bool(h.get("repealed")),
            },
        )


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


def _type_widths(spec: str | None) -> dict[str, int]:
    """"act:3,sl:4" → {"act": 3, "sl": 4}. Bare "act,sl" defaults width 3."""
    out: dict[str, int] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        typ, _, w = part.partition(":")
        out[typ.strip().lower()] = int(w) if w.strip().isdigit() else 3
    return out


# -- id normalisation --------------------------------------------------------
def _title_id(raw: str) -> str:
    """Accept an FRL Title id (``C1901A00002``), a corpus id (``au/cth/act/1901/2``) or a
    legislation.gov.au URL, and return the register Title id the API keys on."""
    raw = (raw or "").strip()
    m = _FRL_ID_RE.match(raw)
    if m:
        return raw.upper()
    m = re.search(r"([CF]\d{4}[A-Z]\d{5})", raw)
    if m:
        return m.group(1).upper()
    m = re.search(r"au/cth/(?P<type>[a-z]+)/(?P<year>\d{4})/(?P<num>\d+)", raw, re.I)
    if m:
        series = {"act": "A", "sl": "L", "ni": "N"}.get(m.group("type").lower(), "A")
        return f"C{m.group('year')}{series}{int(m.group('num')):05d}"
    return raw


def _norm_docid(raw: str) -> str | None:
    """``act-2016-001``, a view URL, or a corpus id → the LawMaker ``docid``.

    A docid or view URL is used **verbatim** — LawMaker's number width is not uniform
    (Qld Acts are 3-digit ``act-2016-001``, Qld subordinate legislation 4-digit
    ``sl-2023-0107``), so reformatting it breaks the fetch. The corpus-id form
    (``au/qld/act/2016/1``) can't recover the exact width, so it's zero-padded to the
    common 4-digit form as a best effort; prefer passing the real docid."""
    raw = (raw or "").strip()
    m = _VIEW_RE.search(raw)
    if m:
        return m.group("docid").lower()
    m = _DOCID_RE.match(raw)
    if m:
        return raw.lower()
    m = re.search(r"au/[a-z]{2,3}/(?P<type>act|sl|sr|si)/(?P<year>\d{4})/(?P<num>\d+[a-z]?)", raw, re.I)
    if m:
        return f"{m.group('type').lower()}-{m.group('year')}-{int(re.sub('[a-z]', '', m.group('num'))):04d}"
    return None
