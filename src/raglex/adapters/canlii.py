"""Canadian case law — the CanLII REST API (metadata + citator; §1.5, §5b).

CanLII's API is deliberately **metadata-only**: it will never return a judgment's
text. What it *does* give — and what nothing else gives for Canada — is:

* **per-case metadata** (title, parallel citations, decision date, docket number,
  subject keywords) plus the canonical CanLII URLs, including the short permalink
  (``canlii.ca/t/1vxsm``) that survives site reorganisations;
* **the citator**: what a case cites (cases *and* legislation) and what cites it,
  extracted by CanLII/Lexum upstream — a structured citation network for the whole
  Canadian collection.

So this adapter complements ``ca-caselaw`` (the A2AJ full-text bulk import) rather
than replacing it. Three uses, matching the three shapes of the corpus's need:

* ``ids=scc/2011/10`` (or "2011 SCC 10") — **targeted**: resolve a pending Canadian
  citation into a *metadata stub* document: name, date, citations and a verified
  "view on CanLII" link, held under the same slug the extractor mints, so every
  citing edge resolves even though the full text stays upstream. This is the path
  the harvest worklist drives.
* ``databases=csc-scc,onca`` — **incremental**: new decisions per court database
  (``publishedAfter`` cursor), as metadata stubs. Keeps the corpus aware of what's
  new after the frozen A2AJ dataset ends.
* :meth:`case_metadata` / :meth:`citator` — the **enrichment surface** the facade's
  ``canlii_enrich`` uses to decorate *held* full-text Canadian decisions with the
  CanLII permalink, keywords, docket number and citator edges.

**Identity.** The caseId CanLII uses embeds the neutral citation with the spaces
squashed (``2008scc9``), so both sides derive the same slug: ``scc/2008/9`` here is
exactly what the citation extractor mints for "2008 SCC 9". Pre-neutral-citation
decisions carry a CanLII id instead ("1980 CanLII 21 (SCC)") whose slug is
``canlii/1980/21`` — again matching the extractor (see ``citations/commonwealth.py``).
The *database* id is usually the neutral court code too, with a handful of federal
exceptions (SCC lives in ``csc-scc``, the Federal Court in ``fct``) mapped in
``DATABASE_OVERRIDES``.

**The governing constraint is politeness, not the network.** CanLII grants keys
individually and its terms cap use (historically ~5,000 calls/day); the quota is
enforced by the same persisted rolling-window ledger CourtListener uses
(``core/api_budget.py``), with deliberately conservative defaults *below* the
documented ceiling, overridable in settings for a key granted more. Every request
is charged before it is made; an exhausted budget surfaces as rate-limiting, which
stops a drain batch and leaves the rest of the queue intact for the next tick.

**Citing-cases cap.** ``citingCases`` for a leading case is enormous (Dunsmuir:
~19,000 rows). The *count* is always recorded (an authority signal, like
CourtListener's ``citation_count``); ``cited_by`` edges are only minted when the
list fits under ``citing_cap`` — a partial slice of an unordered 19k-row list
would be misleading, and the count carries the signal on its own.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from ..core.adapter import BaseAdapter
from ..core.api_budget import BudgetExhausted, RequestBudget, Window
from ..core.errors import FetchError, RateLimitException
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
from .ca_caselaw import report_aliases

BASE_URL = "https://api.canlii.org/v1"
KEY_ENV = "RAGLEX_CANLII_API_KEY"

# Conservative defaults, deliberately below CanLII's documented ceiling (their terms
# have historically allowed ~2 calls/second and 5,000/day). 20/minute paces requests
# 3 seconds apart; 4,000/day leaves headroom for anything else the key is used for.
# Each window is independently overridable in settings for a key granted more.
DEFAULT_WINDOWS = (
    Window("minute", 60.0, 20),
    Window("hour", 3600.0, 900),
    Window("day", 86_400.0, 4000),
)
LIMIT_ENV = {"minute": "RAGLEX_CANLII_PER_MINUTE",
             "hour": "RAGLEX_CANLII_PER_HOUR",
             "day": "RAGLEX_CANLII_PER_DAY"}

_MIN_INTERVAL_FLOOR = 0.5
_UNLIMITED = 10**9
_UNLIMITED_WORDS = {"0", "none", "unlimited", "off", "-1"}

# Neutral court code → CanLII databaseId, where they differ. Everything provincial is
# an identity mapping (onca, bcca, abkb…); the exceptions are federal, where CanLII's
# database carries both languages' names. Verified against the live database list.
DATABASE_OVERRIDES: dict[str, str] = {
    "scc": "csc-scc",       # Supreme Court of Canada
    "fc": "fct",            # Federal Court
    "tcc": "cci-tcc",       # Tax Court of Canada
    "cmac": "cmac-cacm",    # Court Martial Appeal Court
    "citt": "citt-tcce",    # Canadian International Trade Tribunal
    "sst": "sst-tss",       # Social Security Tribunal
    "fpslreb": "pslreb",    # Fed. Public Sector Labour Relations and Employment Board
}


def database_for(court: str) -> str:
    """The CanLII databaseId for a neutral-citation court code. Identity by default —
    CanLII also aliases some codes itself (``scc`` answers for ``csc-scc``) — with the
    known federal exceptions overridden. A wrong guess fails as a clean 404 (absent),
    never as a mis-filed document: the caseId still names the exact decision."""
    court = court.lower()
    return DATABASE_OVERRIDES.get(court, court)


# "2011 SCC 10" / "1980 CanLII 21" as a human writes it (the extractor's raw form).
_RAW_CA_RE = re.compile(
    r"^\s*(?P<year>(?:1[89]|20)\d{2})\s+(?P<court>[A-Za-z]{2,10})\s+(?P<num>\d{1,6})\s*$")
# a slug the extractor mints: scc/2011/10, canlii/1980/21, onscdc/2019/44
_SLUG_CA_RE = re.compile(
    r"^(?P<court>[a-z]{2,10})/(?P<year>(?:1[89]|20)\d{2})/(?P<num>\d{1,6})$", re.I)
# a CanLII caseId: the neutral citation squashed into one token ("2008scc9",
# "1980canlii21"). Non-greedy court so the trailing number stays the number.
_CASE_ID_RE = re.compile(
    r"^(?P<year>(?:1[89]|20)\d{2})(?P<court>[a-z][a-z-]*?)(?P<num>\d{1,6})$", re.I)


def parse_ca_ref(ref: str) -> tuple[str, str, int] | None:
    """``(court, year, num)`` from any of the forms a Canadian case is referred to by:
    the extractor's slug (``scc/2011/10``), the citation as written ("2011 SCC 10"),
    or a CanLII caseId (``2011scc10``)."""
    ref = (ref or "").strip()
    for pattern in (_SLUG_CA_RE, _RAW_CA_RE, _CASE_ID_RE):
        m = pattern.match(ref)
        if m:
            return m.group("court").lower(), m.group("year"), int(m.group("num"))
    return None


def ca_slug(court: str, year: str, num: int) -> str:
    return f"{court.lower()}/{year}/{num}"


def case_id_for(court: str, year: str, num: int) -> str:
    return f"{year}{court.lower()}{num}"


def _localised(value) -> str | None:
    """caseId / aiContentId come back as ``{"en": …}`` or ``{"fr": …}`` in list and
    citator rows, but as a bare string in the per-case call."""
    if isinstance(value, dict):
        return value.get("en") or value.get("fr") or None
    return str(value) if value else None


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _listify(value: str | tuple | list | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


# "RSC 1985, c C-46" → the chapter code Justice Laws (ca-federal) keys by, so a
# citator legislation edge lands on the ca/act/c-46 node the corpus may already hold.
# Only the simple revised-statute form is mapped: supplements ("c 1 (5th Supp)") and
# provincial citations don't correspond to a held id, so those edges stay name-keyed.
_RSC_RE = re.compile(r"^RSC\s+\d{4},?\s+c\s+(?P<ch>[A-Z]-\d+(?:\.\d+)*)$", re.I)


def federal_statute_slug(citation: str | None) -> str | None:
    m = _RSC_RE.match(" ".join((citation or "").split()))
    if not m:
        return None
    return f"ca/act/{m.group('ch').lower()}"


def configured_windows() -> tuple[Window, ...]:
    """The rate-limit windows in force — the conservative defaults unless overridden.
    Same contract as CourtListener's: each window is read independently, 0/none/
    unlimited stops a window binding, an unparseable value falls back to the default."""
    out = []
    for w in DEFAULT_WINDOWS:
        raw = os.environ.get(LIMIT_ENV[w.name], "").strip().lower()
        if raw in _UNLIMITED_WORDS and raw != "":
            out.append(Window(w.name, w.seconds, _UNLIMITED))
            continue
        try:
            limit = int(raw) if raw else w.limit
        except ValueError:
            limit = w.limit
        out.append(Window(w.name, w.seconds, max(1, limit)))
    return tuple(out)


def min_interval_for(windows: tuple[Window, ...]) -> float:
    per_minute = next((w for w in windows if w.name == "minute"), None)
    if not per_minute or per_minute.limit <= 0:
        return 3.0
    return max(_MIN_INTERVAL_FLOOR, per_minute.seconds / per_minute.limit)


def budget_path() -> str | None:
    """The shared ledger beside the catalogue — the same file every raglex process
    spends this key's quota from (see courtlistener.budget_path)."""
    data_dir = os.environ.get("RAGLEX_DATA_DIR", "data")
    return str(Path(data_dir).expanduser() / "api_budget.sqlite")


class CanLIIAdapter(BaseAdapter):
    """CanLII v1 — Canadian case *metadata* and the citator, never full text.

    ``key`` (or ``RAGLEX_CANLII_API_KEY``) is required — CanLII grants keys
    individually via their feedback form. Without one the adapter degrades cleanly:
    discovery raises a clear non-transient error saying how to get one.
    """

    source = "ca-canlii"
    # class default for the orchestrator; the instance recomputes from the windows
    min_interval = 3.0
    requires_js = False
    requires_proxy = False

    def __init__(self, *, key: str | None = None,
                 ids: str | tuple[str, ...] | None = None,
                 databases: str | tuple[str, ...] | None = None,
                 language: str = "en",
                 citator: bool | str | None = None,
                 detail: bool | str = True,
                 citing_cap: int | str = 200,
                 client: RateLimitedClient | None = None,
                 budget: RequestBudget | None = None,
                 base_url: str = BASE_URL) -> None:
        self.key = key or os.environ.get(KEY_ENV) or None
        self.base_url = base_url.rstrip("/")
        self.ids = _listify(ids)
        # the incremental default is the Supreme Court alone: a modest, high-value
        # feed. Widening it is a deliberate operator choice (databases=onca,bcca,…).
        self.databases = _listify(databases) or (["csc-scc"] if not self.ids else [])
        self.language = "fr" if str(language).lower() == "fr" else "en"
        # citator calls cost 2–3 extra requests per case: on by default for a targeted
        # ids fetch (the whole point is enrichment), off for an incremental sweep.
        self.citator_mode = (str(citator).lower() in ("1", "true", "on", "yes")
                             if citator is not None else bool(self.ids))
        # detail=False builds records from the list rows alone (no per-case call) —
        # for a wide stub sweep where 1 request per case would eat the day's budget.
        self.detail = str(detail).lower() not in ("0", "false", "off", "no")
        self.citing_cap = int(citing_cap) if str(citing_cap).strip().isdigit() else 200
        windows = configured_windows()
        self.budget = budget or RequestBudget(self.source, windows, path=budget_path())
        self.min_interval = min_interval_for(self.budget.windows or windows)
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    # -- HTTP ---------------------------------------------------------------
    def _require_key(self) -> None:
        if not self.configured:
            raise FetchError(
                f"{self.source}: no API key — set {KEY_ENV}. CanLII grants keys "
                "individually: apply via canlii.org/en/feedback/feedback.html",
                transient=False)

    def _charge(self, n: int = 1) -> None:
        """Charge the quota before spending it; translated to RateLimitException so
        the drain stops the batch and leaves the queue intact (see courtlistener)."""
        try:
            self.budget.spend(n)
        except BudgetExhausted as exc:
            raise RateLimitException(self.source, retry_after=exc.retry_after) from exc

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        """One API call. A 404 body is ``[{"error": "MISSING", …}]`` — a genuine
        absence (non-transient), reported as such rather than as a failure."""
        self._require_key()
        self._charge()
        query = dict(params or {})
        query["api_key"] = self.key
        resp = self._client.request("GET", f"{self.base_url}{path}", params=query,
                                    raise_for_4xx=False)
        if resp.status_code == 404:
            raise FetchError(f"{self.source}: not found: {path}", transient=False)
        if resp.status_code >= 400:
            # never echo the URL: it carries the API key as a query parameter
            raise FetchError(f"{self.source}: HTTP {resp.status_code} from {path}",
                             transient=resp.status_code >= 500)
        return resp.json()

    # -- discover -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        self._require_key()
        if self.ids:
            yield from self._discover_ids()
        else:
            yield from self._discover_incremental(since, max_pages=max_pages)

    def _discover_ids(self) -> Iterator[Stub]:
        for want in self.ids:
            parsed = parse_ca_ref(want)
            if not parsed:
                continue
            court, year, num = parsed
            if court == "canlii":
                # A bare CanLII number carries no database — the trailing "(ONCA)" of
                # the citation is not in the candidate — so it cannot be fetched
                # directly. It resolves when the citator names it from the other side.
                continue
            yield Stub(
                stable_id=ca_slug(court, year, num),
                court=court,
                hints={"database": database_for(court),
                       "case_id": case_id_for(court, year, num)},
            )

    def _discover_incremental(self, since: str | None, *,
                              max_pages: int | None = None) -> Iterator[Stub]:
        """New decisions per database via ``publishedAfter``. The list rows carry no
        dates at all, so the watermark is the *run* date minus the two-day publication
        lag CanLII's docs recommend — an inclusive re-scan window, deduped by id."""
        page_size = 500
        watermark = (date.today() - timedelta(days=2)).isoformat()
        for db in self.databases:
            offset, pages = 0, 0
            while True:
                params = {"offset": offset, "resultCount": page_size}
                if since:
                    params["publishedAfter"] = (since or "")[:10]
                body = self._get(f"/caseBrowse/{self.language}/{db}/", params)
                rows = body.get("cases") or [] if isinstance(body, dict) else []
                for row in rows:
                    stub = self._stub_for_row(db, row, watermark)
                    if stub is not None:
                        yield stub
                pages += 1
                if len(rows) < page_size or (max_pages is not None and pages >= max_pages):
                    break
                offset += page_size

    def _stub_for_row(self, db: str, row: dict, watermark: str) -> Stub | None:
        case_id = _localised(row.get("caseId"))
        if not case_id:
            return None
        parsed = parse_ca_ref(case_id)
        if parsed:
            stable_id = ca_slug(*parsed)
            court = parsed[0]
        else:
            # a caseId that isn't citation-shaped → held under a surrogate, flagged so
            stable_id = f"ca-case/{db}/{case_id.lower()}"
            court = db
        return Stub(
            stable_id=stable_id,
            title=row.get("title"),
            court=court,
            landing_url=row.get("longUrl"),
            hints={"database": db, "case_id": case_id, "row": row,
                   "watermark": watermark},
        )

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        db = stub.hints.get("database")
        case_id = stub.hints.get("case_id")
        if not db or not case_id:
            return None
        row = stub.hints.get("row")
        if self.detail or row is None:
            meta = self._get(f"/caseBrowse/{self.language}/{db}/{case_id}/")
        else:
            meta = dict(row)
        return self._record(stub, db, case_id, meta)

    def _record(self, stub: Stub, db: str, case_id: str, meta: dict) -> Record:
        citation = (meta.get("citation") or "").strip() or None
        title = meta.get("title") or stub.title or citation or stub.stable_id
        decided = _as_date(meta.get("decisionDate"))
        landing = meta.get("longUrl") or stub.landing_url

        relations: list[TypedRelation] = []
        citator_extra: dict = {}
        if self.citator_mode:
            relations, citator_extra = self.citator_relations(
                db, case_id, exclude=stub.stable_id)

        extra = {
            "jurisdiction": "ca",
            "court_code": (stub.court or db).upper(),
            "canlii_database": db,
            "canlii_case_id": case_id,
            "canlii_url": meta.get("url"),          # the short canlii.ca/t/… permalink
            "citation": citation,
            "docket_number": meta.get("docketNumber"),
            "keywords": meta.get("keywords"),
            "topics": meta.get("topics"),
            # every parallel report citation ("[2008] 1 SCR 190") resolves to this node
            "aliases": report_aliases(citation) or None,
            # CanLII holds the text; we hold the identity, metadata and the link. Say
            # so explicitly — the UI's textless worklist keys off the absent text.
            "metadata_only": True,
            "is_authoritative": False,
            "provider": "CanLII (canlii.org)",
            "canlii_checked_at": date.today().isoformat(),
            "surrogate_id": stub.stable_id.startswith("ca-case/"),
            **citator_extra,
        }
        return Record(
            source=self.source,
            stable_id=stub.stable_id,
            doc_type=DocType.JUDGMENT,
            title=title,
            court=(stub.court or db).lower(),
            decision_date=decided,
            language=meta.get("language") or self.language,
            source_language=meta.get("language") or self.language,
            landing_url=landing,
            text=None,                       # the API never returns judgment text
            relations=relations,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v not in (None, "", [])},
        )

    # -- citator ------------------------------------------------------------
    def citator_relations(self, db: str, case_id: str, *,
                          exclude: str | None = None,
                          include_citing: bool = True) -> tuple[list[TypedRelation], dict]:
        """The citator's three lists as typed edges + the counts for ``extra``.

        * ``citedCases`` → ``mentions`` edges (this case → what it cites);
        * ``citedLegislations`` → ``mentions`` edges to statutes, with federal RSC
          chapters mapped onto the ``ca/act/…`` ids ca-federal holds;
        * ``citingCases`` → deferred ``cited_by`` edges (this case → the later case
          that cites it, the CELLAR pattern) — only when the list fits under
          ``citing_cap``; the count is always recorded.

        Failures are absences here, not errors: a case CanLII has no citator data for
        answers 404 on these endpoints, and metadata without a citator is still worth
        holding. Only rate-limiting propagates.
        """
        relations: list[TypedRelation] = []
        extra: dict = {}
        seen: set[tuple[str, str]] = set()

        def _edges(kind: str) -> list[dict]:
            try:
                body = self._get(f"/caseCitator/en/{db}/{case_id}/{kind}")
            except RateLimitException:
                raise
            except FetchError:
                return []
            return body.get(kind) or [] if isinstance(body, dict) else []

        for row in _edges("citedCases"):
            dst = self._cited_case_slug(row)
            raw = (row.get("citation") or row.get("title") or "").strip() or None
            if not dst or dst == exclude or ("mentions", dst) in seen:
                continue
            seen.add(("mentions", dst))
            relations.append(TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string=raw, dst_id=dst,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
        extra["canlii_cited_count"] = len(relations)

        leg = 0
        for row in _edges("citedLegislations"):
            raw = (row.get("citation") or row.get("title") or "").strip() or None
            dst = federal_statute_slug(row.get("citation"))
            key = ("mentions-leg", dst or (raw or "").casefold())
            if not raw or key in seen:
                continue
            seen.add(key)
            leg += 1
            relations.append(TypedRelation(
                relationship_type=RelationshipType.MENTIONS,
                raw_citation_string=raw, dst_id=dst,
                extracted_via=ExtractedVia.STRUCTURED,
                resolution_status=ResolutionStatus.PENDING))
        extra["canlii_cited_legislation_count"] = leg

        if include_citing:
            citing = _edges("citingCases")
            extra["canlii_citing_count"] = len(citing)
            if len(citing) <= self.citing_cap:
                for row in citing:
                    dst = self._cited_case_slug(row)
                    raw = (row.get("citation") or row.get("title") or "").strip() or None
                    if not dst or dst == exclude or ("cited_by", dst) in seen:
                        continue
                    seen.add(("cited_by", dst))
                    relations.append(TypedRelation(
                        relationship_type=RelationshipType.CITED_BY,
                        raw_citation_string=raw, dst_id=dst,
                        extracted_via=ExtractedVia.STRUCTURED,
                        resolution_status=ResolutionStatus.PENDING))
        return relations, extra

    @staticmethod
    def _cited_case_slug(row: dict) -> str | None:
        """A citator row's caseId ("2004scc28", "1980canlii21") → the extractor's slug
        (``scc/2004/28``, ``canlii/1980/21``), so both sides of every edge speak the
        same identifier the corpus already uses."""
        case_id = _localised(row.get("caseId"))
        parsed = parse_ca_ref(case_id or "")
        if not parsed:
            return None
        return ca_slug(*parsed)

    # -- public helpers ------------------------------------------------------
    def case_metadata(self, ref: str) -> dict | None:
        """Metadata for one case by any Canadian reference form — the facade's
        enrichment building block. None when CanLII doesn't hold it (or the reference
        is a database-less bare CanLII number, which cannot be looked up)."""
        parsed = parse_ca_ref(ref)
        if not parsed or parsed[0] == "canlii":
            return None
        court, year, num = parsed
        try:
            meta = self._get(f"/caseBrowse/{self.language}/{database_for(court)}/"
                             f"{case_id_for(court, year, num)}/")
        except FetchError as exc:
            if exc.transient:
                raise
            return None
        if not isinstance(meta, dict) or not meta.get("caseId"):
            return None
        meta["_database"] = meta.get("databaseId") or database_for(court)
        return meta

    def budget_status(self) -> dict:
        """Live quota state for the settings/maintain panel (same shape as the
        CourtListener one, minus its queue-reserve split)."""
        state = self.budget.state()
        day_used, day_limit = state.windows.get("day", (0, 0))
        uncapped = day_limit >= _UNLIMITED
        return {
            "configured": self.configured,
            "allowed_now": state.allowed,
            "blocked_by": state.blocked_by,
            "retry_after_seconds": round(state.retry_after),
            "remaining": None if state.remaining >= _UNLIMITED else state.remaining,
            "windows": {name: {"used": used,
                               "limit": None if limit >= _UNLIMITED else limit}
                        for name, (used, limit) in state.windows.items()},
            "daily_cap": not uncapped,
            "tier": "default" if configured_windows() == DEFAULT_WINDOWS else "custom",
        }


__all__ = [
    "CanLIIAdapter", "BASE_URL", "KEY_ENV", "DATABASE_OVERRIDES",
    "database_for", "parse_ca_ref", "ca_slug", "case_id_for",
    "federal_statute_slug", "configured_windows", "min_interval_for",
]
