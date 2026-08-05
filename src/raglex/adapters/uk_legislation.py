"""UK legislation adapter — legislation.gov.uk Akoma Ntoso (LegalDocML).

legislation.gov.uk serves clean **Akoma Ntoso** at ``/{type}/{year}/{number}/data.akn``
with point-in-time versions at ``/{type}/{year}/{number}/{YYYY-MM-DD}/...``. The
stable_id is the legislation URI form (``ukpga/2000/36``) — which is exactly what the
§5b resolver mints for a ``legislation.gov.uk`` citation, so harvesting an Act makes its
dangling "cites … s.14" edges resolve, and the AKN gives a structured,
nicely-renderable, machine-readable base.

**Discovery is the search feed by default** — the full-catalogue path. Naming ids
(``-o ids=ukpga/2000/36,…``) fetches exactly those; otherwise ``discover`` walks the
paginated Atom search feed ``/{type}/data.feed?sort=published`` (types combine as
``ukpga+uksi``), newest-published first. An **incremental** run stops at the stored
``<published>`` cursor (new legislation as it is made); a **backfill** (no cursor, no
page cap) walks the feed to its end — the entire back-catalogue of those types.
``types=`` sets which legislation types to walk; ``query=`` is a title search.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator
from xml.etree import ElementTree as ET

from ..core.adapter import BaseAdapter
from ..core.http import RateLimitedClient
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
from .leg_effects import parse_changes_feed, parse_unapplied_effects, summarise_effects

BASE_URL = "https://www.legislation.gov.uk"


def _canonical_leg_id(path: str) -> str:
    """The identity a legislation.gov.uk path is stored under.

    For everything except assimilated EU law this IS the path. Assimilated law is served
    on two of them — ``eur/2016/679`` (the type-code form, which alone has ``/data.akn``
    and dated URIs) and ``european/regulation/2016/0679`` (what the reader, the citation
    grammars and every stored edge use). One instrument, so one node: the canonical form
    wins and the serving form stays in the URL.
    """
    from ..resolve.matchers import assimilated_canonical_path

    return assimilated_canonical_path(path) or path


# Default feed scope: UK-wide primary + secondary legislation. Devolved/NI types can
# be added via ``types=`` (e.g. ``asp,asc,nia,wsi``).
DEFAULT_FEED_TYPES = ("ukpga", "uksi")

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_LEG_NS = "{http://www.legislation.gov.uk/namespaces/legislation}"
_ID_PATH = re.compile(r"legislation\.gov\.uk/id/(?P<path>[a-z]{2,6}/[^\s?#]+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Ns:
    """Minimal stand-in for a Stub so record_from_akn can share fetch's body
    whether the AKN came from the feed or a manual upload."""
    stable_id: str
    landing_url: str | None
    hints: dict


@dataclass(frozen=True, slots=True)
class FeedEntry:
    path: str            # ukpga/2026/12 — the stable_id / fetch path
    title: str | None
    published: str | None  # full ISO timestamp (the feed's sort key → the cursor)
    updated: str | None


@dataclass(frozen=True, slots=True)
class FeedPage:
    entries: list[FeedEntry]
    more_pages: bool


def parse_legislation_feed(xml_bytes: bytes) -> FeedPage:
    """Parse one legislation.gov.uk search-feed page (pure). Entries carry their
    ``/id/{type}/{year}/{number}`` URI, title, and published/updated timestamps;
    ``<leg:morePages>`` says whether to keep paging."""
    root = ET.fromstring(xml_bytes)
    more = 0
    mp = root.findtext(f"{_LEG_NS}morePages")
    page = root.findtext(f"{_LEG_NS}page")
    try:
        more = int(mp or 0) > int(page or 1)
    except ValueError:
        more = False
    entries: list[FeedEntry] = []
    for entry in root.findall(f"{_ATOM_NS}entry"):
        eid = (entry.findtext(f"{_ATOM_NS}id") or "").strip()
        m = _ID_PATH.search(eid)
        if not m:
            continue
        entries.append(FeedEntry(
            path=m.group("path").strip("/").lower(),
            title=(entry.findtext(f"{_ATOM_NS}title") or "").strip() or None,
            published=(entry.findtext(f"{_ATOM_NS}published") or "").strip() or None,
            updated=(entry.findtext(f"{_ATOM_NS}updated") or "").strip() or None,
        ))
    return FeedPage(entries=entries, more_pages=more)


def _iso_date(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class UKLegislationAdapter(BaseAdapter):
    source = "uk-legislation"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 version_date: str | None = None, client: RateLimitedClient | None = None,
                 patient: bool = False, feed: str | None = None,
                 types: str | None = None, query: str | None = None) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        # Feed mode is the DEFAULT: with no explicit ids, discover() walks the
        # newest-published search feed over ``types`` — so an incremental run pulls new
        # legislation and a ``--backfill`` (no cursor, no page cap) walks the entire
        # back-catalogue. Naming ids switches to fetching exactly those. ``types`` limits
        # the legislation types ("ukpga,uksi"); ``query`` is a title search.
        self.feed = bool(feed) or bool(types) or bool(query) or not self.ids
        self.types = tuple(t.strip().lower() for t in (types or "").split(",") if t.strip()) \
            or DEFAULT_FEED_TYPES
        self.query = (query or "").strip() or None
        # point-in-time: fetch the law as it stood at this date (YYYY-MM-DD), so a
        # citation from an old case sees the live provisions, not today's repealed text.
        self.version_date = version_date
        # Fail FAST by default: a few very large Acts (e.g. FSMA 2000) make
        # legislation.gov.uk take minutes generating /data.akn. With the default 5×30s
        # retries one such Act blocks a bulk harvest — so cap retries/timeout; a hang
        # gives up in ~30s and the caller records it as a miss and moves on.
        # ``patient`` (the SINGLE-item harvest path) is the opposite trade: the user
        # asked for exactly this Act, and the biggest Acts are precisely the ones the
        # fast path can never fetch — wait for the render instead of failing forever.
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval,
            max_retries=2 if patient else 1, timeout=180 if patient else 25)

    def changes_affecting(self, stable_id: str, *, max_pages: int = 20) -> list:
        """The affecting-side "Changes to Legislation" feed for an act: every change it
        makes to *other* legislation (``/changes/affecting/{id}/data.feed``, paged). This
        is how a freshly-imported amending act enumerates what it changes — so the change
        can be pushed to the affected instruments rather than waiting for them to be
        re-pulled. Returns ``ChangeEffect``s; tolerant of network/parse failure (→ [])."""
        base = stable_id.split("@")[0]
        out: list = []
        for page in range(1, max_pages + 1):
            url = (f"{BASE_URL}/changes/affecting/{base}/data.feed"
                   f"?results-count=500&sort=affected-year-number&page={page}")
            try:
                resp = self._client.get(url)
            except FetchError:
                break
            effs = parse_changes_feed(resp.content)
            if not effs:
                break  # past the last page
            out.extend(effs)
            if len(effs) < 500:
                break
        return out

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if self.feed:
            yield from self._discover_feed(since, max_pages=max_pages)
            return
        from ..resolve.matchers import assimilated_leg_path

        for requested in self.ids:
            # A caller may name assimilated law either way ("-o ids=eur/2016/679");
            # both mean one instrument, so both key to the canonical identity.
            leg_id = _canonical_leg_id(requested)
            if self.version_date:  # point-in-time copy, keyed distinctly as id@date
                # The IDENTITY stays the id the corpus knows; only the URI moves.
                # Assimilated EU law is keyed european/regulation/2016/0679, whose
                # dated representations 404 — the same instrument serves them under
                # eur/2016/679. Keying the fetched copy off the requested id keeps
                # `european/regulation/2016/0679@2024-01-01` pointing at its base.
                uri_path = assimilated_leg_path(leg_id) or leg_id
                yield Stub(
                    stable_id=f"{leg_id}@{self.version_date}",
                    landing_url=f"{BASE_URL}/{uri_path}/{self.version_date}",
                    raw_url=f"{BASE_URL}/{uri_path}/{self.version_date}/data.akn",
                    hints={"base_id": leg_id, "version_date": self.version_date},
                )
            else:
                yield Stub(
                    stable_id=leg_id,
                    landing_url=f"{BASE_URL}/{leg_id}",
                    raw_url=f"{BASE_URL}/{leg_id}/data.akn",
                    court=None,
                )

    def _discover_feed(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Walk the search feed newest-published first, stopping at the incremental
        cursor. ``sort=published`` makes ``<published>`` the sort key, so it is also the
        cursor field — the crawl stops exactly where the last clean run got to.

        A title-query feed redirects to ``/title/{q}/data.feed`` and DROPS the sort —
        its order isn't publication date — so with a query the cursor is not applied
        (each crawl re-walks its bounded pages; already-held items dedup cheaply)."""
        from ..citations.snowball import UK_LEG_TYPES

        type_path = "+".join(self.types) if self.types else "all"
        pages = 0
        page_no = 1
        while True:
            params: dict[str, object] = {"sort": "published", "page": page_no}
            if self.query:
                params["title"] = self.query
            try:
                resp = self._client.get(f"{BASE_URL}/{type_path}/data.feed", params=params)
            except FetchError:
                return  # a broken feed page ends the crawl; the cursor doesn't advance past it
            feed = parse_legislation_feed(resp.content)
            for e in feed.entries:
                if not self.query and since and e.published and e.published <= since:
                    return
                head = e.path.split("/", 1)[0]
                if head not in UK_LEG_TYPES:
                    continue  # drafts / impact assessments / non-legislation ids
                yield Stub(
                    # IDENTITY is canonical, the URI is whatever serves it. Assimilated
                    # EU law answers on two paths — legislation.gov.uk's feeds emit the
                    # type-code form (eur/2016/679) while citations, the reader and the
                    # grammars all use european/regulation/2016/0679 — and minting the
                    # feed's form kept the corpus holding both. 4,171 assimilated
                    # instruments ended up stored twice, the UK GDPR among them, with
                    # 40,042 citations landing on the copy nothing else pointed at.
                    stable_id=_canonical_leg_id(e.path),
                    landing_url=f"{BASE_URL}/{e.path}",
                    raw_url=f"{BASE_URL}/{e.path}/data.akn",
                    hint_date=_iso_date(e.published),
                    title=e.title,
                    hints={"watermark": e.published} if (e.published and not self.query) else {},
                )
            pages += 1
            if not feed.more_pages or (max_pages is not None and pages >= max_pages):
                return
            page_no += 1

    def fetch(self, stub: Stub) -> Record | None:
        # Assimilated EU law (/european/…) isn't served at /data.akn — it needs AKN
        # content negotiation on the base URL.
        is_assim = stub.stable_id.lower().startswith("european/")
        # …EXCEPT for a dated ask, where discover() has already built the URI under the
        # type-code path that does serve representations (eur/2016/679/2024-01-01/
        # data.akn). Content-negotiating the id instead produced HTTP 400, because the
        # id carries the "@date" that belongs in the path.
        version_date = stub.hints.get("version_date")
        if is_assim and not version_date:
            url = f"{BASE_URL}/{stub.stable_id}"
            headers = {"Accept": "application/akn+xml", "Accept-Language": "en"}
        else:
            url, headers = stub.raw_url, None
        # legislation.gov.uk *async-generates* large representations: it answers 202 with
        # an empty body while building them. Retry a few times; if it never materialises,
        # that's a TRANSIENT failure (the item exists, the server is still building it) —
        # raising it as such keeps the item in the worklist instead of writing it off as
        # absent for months. A 404/410 raises a fatal FetchError from the client.
        raw = b""
        try:
            for attempt in range(4):
                resp = self._client.get(url, headers=headers) if headers else self._client.get(url)
                raw = resp.content or b""
                if raw and getattr(resp, "status_code", 200) != 202:
                    break
                time.sleep(2 * (attempt + 1))
        except FetchError as exc:
            # Large domestic Acts can make the on-demand AKN renderer time out/504 while
            # the publisher's canonical CLML representation is already available (FSMA
            # 2000 is ~21 MB and serves data.xml in under a second). CLML carries the same
            # operative hierarchy and effects metadata, so use it instead of cooling a
            # live Act merely because one presentation format is slow to generate.
            if not exc.transient or is_assim:
                raise
            fallback = self._domestic_clml_fallback(stub, url)
            if fallback is not None:
                return fallback
            raise exc
        if not raw:
            # HTTP 202 is another face of the same AKN-rendering problem. Do not put a
            # live Act on cooldown if its already-materialised CLML can be read now.
            if not is_assim:
                fallback = self._domestic_clml_fallback(stub, url)
                if fallback is not None:
                    return fallback
            raise FetchError(
                f"{self.source}: {url} still generating (HTTP 202) after 4 attempts",
                transient=True,
            )
        # Assimilated EU regs: the AKN body is empty (only TOC + recitals) — the operative
        # articles live only in the CLML data.xml, and only under the CANONICAL eur/… path
        # (the /european/… alias serves an empty CLML too). The AKN's FRBRWork carries that
        # canonical id, so pull the CLML body from it and let record_from_akn use it for
        # text+segments while the AKN still supplies effects/currency/title.
        clml_body = None
        if is_assim:
            from ..formats.akoma_ntoso import _frbr_work_id
            eur_id = _frbr_work_id(raw)
            if eur_id:
                # the CLML of the SAME expression — an undated body would silently
                # reintroduce today's text into a dated record
                clml_url = (f"{BASE_URL}/{eur_id}/{version_date}/data.xml" if version_date
                            else f"{BASE_URL}/{eur_id}/data.xml")
                try:
                    r = self._client.get(clml_url)
                    if r.content and b"<P1group" in r.content:
                        clml_body = r.content
                except Exception:  # noqa: BLE001 — fall back to AKN-only on any CLML hiccup
                    clml_body = None
        return self.record_from_akn(
            stub.stable_id, raw, clml_body=clml_body,
            landing_url=stub.landing_url,
            base_id=stub.hints.get("base_id"),
            version_date=stub.hints.get("version_date"))

    def _domestic_clml_fallback(self, stub: Stub, akn_url: str) -> Record | None:
        """Read the publisher's other authoritative XML rendition when AKN is slow.

        A failure here must not replace the original transient AKN failure with (for
        example) a fatal CLML 404: returning ``None`` lets the caller preserve the
        correct retry classification.
        """
        clml_url = akn_url.removesuffix("data.akn") + "data.xml"
        try:
            clml = self._client.get(clml_url)
        except Exception:  # noqa: BLE001 - the original AKN outcome remains authoritative
            return None
        if not clml.content:
            return None
        try:
            record = self.record_from_akn(
                stub.stable_id, clml.content, landing_url=stub.landing_url,
                base_id=stub.hints.get("base_id"),
                version_date=stub.hints.get("version_date"),
                source_format="clml",
            )
        except Exception:  # malformed/unsupported CLML: retain the normal retry path
            return None
        return record if record.text else None

    def record_from_akn(self, stable_id: str, raw: bytes, *,
                        clml_body: bytes | None = None,
                        landing_url: str | None = None,
                        base_id: str | None = None,
                        version_date: str | None = None,
                        source_format: str = "akoma-ntoso") -> Record:
        """Build a legislation Record from an authoritative XML rendition. Shared by
        live harvest and manual upload, so a hand-supplied AKN file for an instrument
        legislation.gov.uk won't serve gets the same parse as a harvested one. Domestic
        CLML is accepted as a fallback when the publisher's AKN renderer times out.

        ``clml_body`` (assimilated EU regs): the AKN body is empty, so its CLML
        data.xml is supplied separately — its text+segments replace the AKN's while
        the AKN still drives effects/currency/title (both are namespace-agnostic)."""
        parsed = parse(source_format, raw)
        extra: dict = {"format": source_format}
        if clml_body:
            clml_pd = parse("clml", clml_body)
            if clml_pd.segments:
                parsed.text = clml_pd.text
                parsed.segments = clml_pd.segments
                extra["format"] = "clml-body+akn-meta"
        title = parsed.title or stable_id
        relations = list(parsed.relations)
        stub = _Ns(stable_id=stable_id,
                   landing_url=landing_url or f"{BASE_URL}/{stable_id}",
                   hints={"base_id": base_id, "version_date": version_date})
        # point-in-time copy → mark the title and link to the base instrument
        base_id = stub.hints.get("base_id")
        # Outstanding amendments (§0): the editorial lag is in the XML. Skip this for
        # point-in-time copies — the effects machinery is attached only to the
        # current/revised view, not a dated snapshot (legislation.gov.uk docs).
        if not base_id:
            effects = parse_unapplied_effects(raw)
            summary = summarise_effects(effects)
            # always record the summary (even when zero) so a re-harvest can *clear* an
            # instrument whose effects have since been incorporated.
            extra["unapplied_effects"] = summary
            # One edge per distinct effect, carrying as much metadata as the source gives:
            # src_anchor = which provision of THIS act is changed; dst_anchor = the kind of
            # change (repealed/inserted/…). The edge is directional (this act ← amended_by ←
            # the amending act), but the graph reads it both ways: the amending act's
            # *incoming* amended_by edges enumerate everything it changes (facade
            # effects_caused_by), so we don't duplicate the fact on both nodes.
            seen: set[tuple[str, str | None, str | None]] = set()
            for e in effects:
                target = e.affecting_id or e.commencing_id
                key = (target or "", e.affected_ref, e.type)
                if not target or target == stub.stable_id or key in seen:
                    continue  # need a target; don't self-link; dedupe identical effects
                seen.add(key)
                relations.append(TypedRelation(
                    relationship_type=RelationshipType.AMENDED_BY,
                    raw_citation_string=target, dst_id=target,
                    src_anchor=e.affected_ref, dst_anchor=e.type,
                    extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.PENDING,
                ))
        # Unified legislative currency (§CUR): the UK's editorial-lag + point-in-time model,
        # mapped onto the canonical shape so the status banner treats it like every other
        # jurisdiction. The force status itself is left to the change-edge graph (repeals/
        # amendments); here we carry the point-in-time affordance, the unapplied backlog, and
        # per-provision markers from the effects.
        from ..leg_currency import Currency, Provision, CanonStatus
        cur = Currency(scheme="uk-leg", point_in_time_capable=True)
        if base_id:
            cur.as_at = stub.hints.get("version_date")
            cur.status = str(CanonStatus.CONSOLIDATED)   # a dated point-in-time snapshot
        else:
            # legislation.gov.uk occasionally communicates whole-instrument currency
            # only in the canonical title (for example "Race Relations Act 1976
            # (Repealed)") while the AKN exposes no separate document-status field.
            # That explicit publisher label is stronger than leaving the UI/MCP at
            # "currency unconfirmed".
            if re.search(r"\((?:repealed|revoked)\)\s*$", title, re.IGNORECASE):
                cur.status = str(CanonStatus.REPEALED)
            outstanding = summary.get("outstanding", 0)
            cur.unapplied_count = outstanding
            cur.up_to_date = (outstanding == 0)
            prov: dict[str, Provision] = {}
            for e in effects:
                ref = (e.affected_ref or "").strip()
                if not ref:
                    continue
                p = prov.setdefault(ref, Provision(anchor=ref))
                if e.type and e.type not in p.change_types:
                    p.change_types.append(e.type)
                if e.affecting_id and e.affecting_id not in p.changed_by:
                    p.changed_by.append(e.affecting_id)
            cur.provisions = list(prov.values())
        cur_meta = cur.to_meta()
        if cur_meta:
            extra["currency"] = cur_meta
        if base_id:
            title = f"{title} (as at {stub.hints.get('version_date')})"
            relations.append(TypedRelation(
                relationship_type=RelationshipType.POINT_IN_TIME_OF,
                raw_citation_string=base_id, dst_id=base_id,
                extracted_via=ExtractedVia.STRUCTURED, resolution_status=ResolutionStatus.PENDING,
            ))
        # Assimilated EU law (legislation.gov.uk /european/… OR the type-code form
        # eur/eudr/eudn/…): mark the title and link it to the EU original it's an
        # assimilated version of — don't conflate them.
        head = stub.stable_id.split("/", 1)[0].lower()
        if stub.stable_id.lower().startswith("european/") or head in {"eur", "eudr", "eudn", "eudc", "eufr"}:
            if title and not title.lower().startswith("assimilated"):
                title = f"Assimilated {title}"
            from ..resolve.matchers import assimilated_celex
            celex = assimilated_celex(stub.stable_id)
            if celex:
                relations.append(TypedRelation(
                    relationship_type=RelationshipType.ASSIMILATED_VERSION_OF,
                    raw_citation_string=celex, dst_id=celex,
                    extracted_via=ExtractedVia.STRUCTURED,
                    resolution_status=ResolutionStatus.PENDING,
                ))
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=title,
            language="en",
            source_language="en",
            landing_url=stub.landing_url,
            raw_bytes=raw,
            raw_ext="xml",
            text=parsed.text,
            segments=parsed.segments,
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra=extra,
        )
