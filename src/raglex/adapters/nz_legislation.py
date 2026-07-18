"""New Zealand legislation — the PCO Developer API (v0).

New Zealand is a single unitary jurisdiction with an unusually clean data story, and one
hard operational constraint that dictates the entire design:

    **The website is bot-walled.** Plain *and* stealth GETs to
    ``www.legislation.govt.nz`` return HTTP 405 "Human Verification". So does
    ``catalogue.data.govt.nz``, which hosts the bulk XML. There is therefore **no HTML
    scraping path here, and none should be added** — defeating the challenge is fragile
    and discourteous when the publisher offers a sanctioned API.

Everything goes through the **Developer API** (``api.legislation.govt.nz``), which needs
an API key (``RAGLEX_NZ_API_KEY``). Without one the adapter degrades cleanly: discovery
yields nothing and says why, rather than falling back to scraping.

The API mirrors FRBR, which maps directly onto the corpus model used throughout:

* **Work** (``act_public_1990_109``) → the enduring instrument → the stable_id.
* **Version** (``act_public_1990_109_en_2022-08-30``) → a consolidation at a point in
  time → an Expression. NZ's point-in-time is native: each consolidation is its own
  addressable version.
* **Format** → the XML/PDF/HTML manifestation; the API hands over the URLs, so content
  is fetched by following what the API returns rather than by constructing website URLs.

**Rate limits are real and documented**: 10,000 requests/key/day, plus a 2,000-per-5-min
per-IP burst ceiling that returns 403 (not 429). ``min_interval`` is set to keep a
long backfill inside the burst ceiling by construction.

The XML schema itself is PCO-specific and, as of writing, **unverified against a live
sample** — see ``formats.nz_pco_xml``, which infers structure by shape and always falls
back to whole-text capture so an unexpected schema costs structure, never content.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterator

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
from ..formats.nz_pco_xml import nz_id, parse_nz_pco_xml

__all__ = ["nz_id", "NZLegislationAdapter", "parse_work_id", "WorkId"]

API = "https://api.legislation.govt.nz"
API_KEY_ENV = "RAGLEX_NZ_API_KEY"

# The PCO's six-segment identifier grammar. Segments may be prefixed "~" when the value
# is ephemeral (harvested agency material where the real value isn't known yet) — those
# ids can CHANGE upstream, so they are flagged rather than trusted as permanent.
_WORK_ID_RE = re.compile(
    r"^(?P<type>act|bill|secondary-legislation|amendment-paper)_"
    r"(?P<subtype>[a-z-]+)_"
    r"(?P<year>~?\d{4})_"
    r"(?P<number>~?\d+[A-Za-z]?(?:-[A-Za-z])?)$", re.I)
_VERSION_SUFFIX_RE = re.compile(r"_(?P<lang>[a-z]{2})_(?P<version_date>~?[\d-]+[A-Z]*)$", re.I)


@dataclass(frozen=True, slots=True)
class WorkId:
    type: str
    subtype: str
    year: str
    number: str
    language: str | None = None
    version_date: str | None = None

    @property
    def ephemeral(self) -> bool:
        """PCO marks unstable segments with ``~``; such ids may change upstream."""
        return any("~" in p for p in (self.year, self.number, self.version_date or ""))

    @property
    def stable_id(self) -> str:
        return nz_id(self.type, self.subtype, self.year, self.number,
                     self.language or "en")


def parse_work_id(raw: str) -> WorkId | None:
    """``act_public_1990_109`` or a full version_id → a ``WorkId``."""
    raw = (raw or "").strip()
    lang = version_date = None
    m = _VERSION_SUFFIX_RE.search(raw)
    if m:
        lang, version_date = m.group("lang").lower(), m.group("version_date")
        raw = raw[:m.start()]
    m = _WORK_ID_RE.match(raw)
    if not m:
        return None
    return WorkId(type=m.group("type").lower(), subtype=m.group("subtype").lower(),
                  year=m.group("year"), number=m.group("number"),
                  language=lang, version_date=version_date)


class NZLegislationAdapter(BaseAdapter):
    """PCO Developer API — Acts, secondary legislation, Bills and amendment papers.

    Discovery pages ``/v0/works/`` (optionally filtered by type/status/agency or a
    search term); each result carries its ``latest_matching_version`` with format URLs,
    so a stub already knows where its content lives. ``fetch`` pulls the XML format and
    parses it.

    ``sort_by=most_recently_updated`` makes the incremental path work the way the other
    feed-like sources do: walk newest-first and stop at the watermark.
    """

    source = "nz-legislation"
    # 2,000 requests / 5 min per IP is a hard 403 ceiling → 0.2s floor keeps a backfill
    # under it with margin, while still allowing ~5 req/s.
    min_interval = 0.2
    requires_js = False
    requires_proxy = False

    def __init__(self, *, api_key: str | None = None, ids: str | tuple[str, ...] | None = None,
                 legislation_type: str = "act", query: str | None = None,
                 status: str | None = None, agency: str | None = None,
                 per_page: int = 100, client: RateLimitedClient | None = None) -> None:
        self.api_key = api_key or os.environ.get(API_KEY_ENV) or None
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else ()
        self.legislation_type = (legislation_type or "").strip().lower() or None
        self.query = (query or "").strip() or None
        self.status = (status or "").strip() or None
        self.agency = (agency or "").strip() or None
        self.per_page = max(1, min(int(per_page), 100))
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=60)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """One API call. The key goes in the ``X-Api-Key`` header rather than the
        ``api_key`` query parameter (both are accepted) so it never lands in a URL that
        might be logged."""
        try:
            resp = self._client.get(f"{API}{path}", params=params or {},
                                    headers={"X-Api-Key": self.api_key or ""})
            return json.loads(resp.content)
        except (FetchError, json.JSONDecodeError, TypeError):
            return None

    # -- discovery -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if not self.configured:
            # No key → no sanctioned channel. Yield nothing rather than scraping the
            # bot-walled website; the UI surfaces the missing setting.
            return
        if self.ids:
            yield from self._discover_ids()
            return

        params = {"per_page": self.per_page,
                  "sort_by": "most_recently_updated" if since else "year_desc"}
        if self.legislation_type:
            params["legislation_type"] = self.legislation_type
        if self.query:
            params["search_term"] = self.query
            params["search_field"] = "title"
        if self.status:
            params["legislation_status"] = self.status
        if self.agency:
            params["administering_agencies"] = self.agency

        page = 1
        while True:
            data = self._get("/v0/works/", {**params, "page": page})
            if not data:
                return
            results = data.get("results") or []
            if not results:
                return
            for row in results:
                stub = self._stub(row)
                if stub is not None:
                    yield stub
            total = int(data.get("total") or 0)
            if page * self.per_page >= total:
                return
            page += 1
            if max_pages is not None and page > max_pages:
                return

    def _discover_ids(self) -> Iterator[Stub]:
        for raw in self.ids:
            work = parse_work_id(raw)
            if work is None:
                continue
            data = self._get(f"/v0/works/{_api_work_id(raw)}/versions/", {"sort": "desc"})
            rows = (data or {}).get("results") or []
            if rows:
                stub = self._stub({**rows[0], "work_id": _api_work_id(raw),
                                   "latest_matching_version": rows[0]})
                if stub is not None:
                    yield stub

    def _stub(self, row: dict) -> Stub | None:
        work_id = row.get("work_id") or ""
        work = parse_work_id(work_id)
        if work is None:
            return None
        version = row.get("latest_matching_version") or row
        formats = {f.get("type", "").lower(): f.get("url")
                   for f in (version.get("formats") or []) if f.get("url")}
        version_id = version.get("version_id") or ""
        parsed_version = parse_work_id(version_id)
        return Stub(
            stable_id=work.stable_id,
            title=version.get("title") or row.get("title"),
            landing_url=f"https://www.legislation.govt.nz/{work.type}/{work.subtype}/"
                        f"{work.year}/{work.number}/latest/",
            raw_url=formats.get("xml"),
            hint_date=_version_date(parsed_version),
            hints={"work_id": work_id, "version_id": version_id,
                   "formats": formats,
                   "ephemeral": work.ephemeral or bool(parsed_version and parsed_version.ephemeral),
                   "legislation_type": row.get("legislation_type"),
                   "legislation_status": row.get("legislation_status"),
                   "act_type": row.get("act_type"), "act_status": row.get("act_status"),
                   "instrument_status": row.get("instrument_status"),
                   "instrument_type_group": row.get("instrument_type_group"),
                   "classification": row.get("act_classification")
                   or row.get("instrument_classification"),
                   "agencies": row.get("administering_agencies") or [],
                   "watermark": version_id},
        )

    # -- fetch ---------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        if not self.configured:
            return None
        h = stub.hints
        url = h.get("formats", {}).get("xml") or stub.raw_url
        if not url:
            return None
        try:
            resp = self._client.get(url, headers={"X-Api-Key": self.api_key or ""})
        except FetchError:
            return None
        data = resp.content or b""
        doc = parse_nz_pco_xml(data)
        if not doc.text:
            return None

        version = parse_work_id(h.get("version_id") or "")
        relations: list[TypedRelation] = []
        # The version is a point-in-time Expression of the Work; record that edge so an
        # older case can cite the text as it then stood rather than today's.
        if version and version.version_date:
            relations.append(TypedRelation(
                relationship_type=RelationshipType.POINT_IN_TIME_OF,
                raw_citation_string=h.get("version_id"),
                dst_id=stub.stable_id, dst_anchor=version.version_date,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING,
            ))

        extra = {
            "jurisdiction": "nz",
            "format": "nz-pco-xml",
            "work_id": h.get("work_id"),
            "version_id": h.get("version_id"),
            "legislation_type": h.get("legislation_type"),
            "legislation_status": h.get("legislation_status"),
            "act_type": h.get("act_type"),
            "act_status": h.get("act_status"),
            "instrument_status": h.get("instrument_status"),
            "instrument_type_group": h.get("instrument_type_group"),
            "classification": h.get("classification"),
            "administering_agencies": h.get("agencies") or None,
            "is_authoritative": True,     # the PCO is the official NZ source
            "point_in_time": (version.version_date if version else None),
            "formats_available": sorted(h.get("formats", {})) or None,
            # PCO "~" ids are not permanent and may change upstream — never present one
            # as a settled identifier.
            "ephemeral_id": h.get("ephemeral") or None,
            # the schema wasn't verifiable pre-API-key; says how the text was recovered
            "inferred_structure": doc.metadata.get("inferred_structure"),
            "unverified_schema": doc.metadata.get("unverified_schema"),
        }

        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.LEGISLATION,
            title=stub.title or doc.title or stub.stable_id,
            language="en", source_language="en",
            decision_date=doc.decision_date,
            landing_url=stub.landing_url,
            raw_bytes=data, raw_ext="xml",
            text=doc.text, segments=doc.segments, relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v is not None},
        )


def _api_work_id(raw: str) -> str:
    work = parse_work_id(raw)
    if work is None:
        return raw
    return f"{work.type}_{work.subtype}_{work.year}_{work.number}"


def _version_date(version: WorkId | None) -> date | None:
    if version is None or not version.version_date:
        return None
    try:
        return date.fromisoformat(version.version_date.lstrip("~")[:10])
    except ValueError:
        return None
