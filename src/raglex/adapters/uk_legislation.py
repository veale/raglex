"""UK legislation adapter — legislation.gov.uk Akoma Ntoso (LegalDocML).

Harvesting statute, not just cases (§0). legislation.gov.uk serves clean
**Akoma Ntoso** at ``/{type}/{year}/{number}/data.akn`` with point-in-time
versions at ``/{type}/{year}/{number}/{YYYY-MM-DD}/...``. The stable_id is the
legislation URI form (``ukpga/2000/36``) — which is exactly what the §5b resolver
mints for a ``legislation.gov.uk`` citation, so harvesting FOIA/DPA makes all the
dangling "cites FOIA s.14" edges resolve, and the AKN gives a structured,
nicely-renderable, machine-readable base.

Default targets are the core UK data-protection / FOI instruments; override with
``-o ids=ukpga/2000/36,ukpga/2018/12`` or point it at any list.
"""

from __future__ import annotations

import time
from typing import Iterator

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
from .leg_effects import parse_unapplied_effects, summarise_effects

BASE_URL = "https://www.legislation.gov.uk"

# Core UK information-rights statutes (§3/§4 focus).
DEFAULT_IDS = (
    "ukpga/2000/36",   # Freedom of Information Act 2000
    "ukpga/2018/12",   # Data Protection Act 2018
    "uksi/2004/3391",  # Environmental Information Regulations 2004
)


class UKLegislationAdapter(BaseAdapter):
    source = "uk-legislation"
    min_interval = 0.5
    requires_js = False
    requires_proxy = False

    def __init__(self, *, ids: str | tuple[str, ...] | None = None,
                 version_date: str | None = None, client: RateLimitedClient | None = None) -> None:
        if isinstance(ids, str):
            ids = tuple(i.strip() for i in ids.split(",") if i.strip())
        self.ids = tuple(ids) if ids else DEFAULT_IDS
        # point-in-time: fetch the law as it stood at this date (YYYY-MM-DD), so a
        # citation from an old case sees the live provisions, not today's repealed text.
        self.version_date = version_date
        # Fail FAST: a few very large Acts (e.g. FSMA 2000) make legislation.gov.uk hang
        # generating /data.akn. With the default 5×30s retries one such Act blocks a bulk
        # harvest for minutes — so cap retries/timeout; a hang gives up in ~30s and the
        # caller records it as a miss and moves on.
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, max_retries=1, timeout=25)

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
        for leg_id in self.ids:
            if self.version_date:  # point-in-time copy, keyed distinctly as id@date
                yield Stub(
                    stable_id=f"{leg_id}@{self.version_date}",
                    landing_url=f"{BASE_URL}/{leg_id}/{self.version_date}",
                    raw_url=f"{BASE_URL}/{leg_id}/{self.version_date}/data.akn",
                    hints={"base_id": leg_id, "version_date": self.version_date},
                )
            else:
                yield Stub(
                    stable_id=leg_id,
                    landing_url=f"{BASE_URL}/{leg_id}",
                    raw_url=f"{BASE_URL}/{leg_id}/data.akn",
                    court=None,
                )

    def fetch(self, stub: Stub) -> Record | None:
        # Assimilated EU law (/european/…) isn't served at /data.akn — it needs AKN
        # content negotiation on the base URL.
        is_assim = stub.stable_id.lower().startswith("european/")
        url = f"{BASE_URL}/{stub.stable_id}" if is_assim else stub.raw_url
        headers = {"Accept": "application/akn+xml", "Accept-Language": "en"} if is_assim else None
        # legislation.gov.uk *async-generates* large representations: it answers 202 with
        # an empty body while building them. Retry a few times; if it never materialises,
        # that's a TRANSIENT failure (the item exists, the server is still building it) —
        # raising it as such keeps the item in the worklist instead of writing it off as
        # absent for months. A 404/410 raises a fatal FetchError from the client.
        raw = b""
        for attempt in range(4):
            resp = self._client.get(url, headers=headers) if headers else self._client.get(url)
            raw = resp.content or b""
            if raw and getattr(resp, "status_code", 200) != 202:
                break
            time.sleep(2 * (attempt + 1))
        if not raw:
            raise FetchError(
                f"{self.source}: {url} still generating (HTTP 202) after 4 attempts",
                transient=True,
            )
        parsed = parse("akoma-ntoso", raw)
        title = parsed.title or stub.stable_id
        relations = list(parsed.relations)
        extra: dict = {"format": "akoma-ntoso"}
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
