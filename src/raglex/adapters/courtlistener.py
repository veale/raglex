"""US case law — CourtListener (Free Law Project) REST API v4 (§1.5).

The corpus already *recognises* US reporter citations (``citations/us_cases.py``
mints ``us/<reporter>/<vol>/<page>`` for "576 U.S. 644"); this adapter is what makes
them fetchable. It stores each decision under that same slug, so harvesting a case
resolves every pending edge that cites it — in either reporter, because the parallel
citations become aliases.

**The governing constraint is the rate limit, not the network.** The free tier gives
5 requests/minute, 50/hour and *125 per day*, all rolling concurrently. That is
enough for on-demand lookups and a slow drip of the citation backlog; it is nowhere
near enough to walk a court's history. Whole-court seeding is the bulk importer's job
(``courtlistener_bulk.py``, no rate limit at all) and this adapter deliberately
offers no "harvest everything" mode.

The quota is enforced by a **persisted rolling-window ledger**
(``core/api_budget.py``), not just by pacing: every request is charged against the
day's 125 before it is made, and a request that doesn't fit raises rather than
earning a 429. The ledger survives process restarts, because the windows are hours
long and a job is minutes long — an in-process counter would reset the budget on
every run and blow through the daily cap by an order of magnitude. When the budget
runs out mid-batch the adapter reports it as rate-limiting, which the harvest drain
already understands: it stops the batch and leaves the rest of the queue untouched
(rather than marking it all dead), so the next tick picks up where this one stopped.

Three ways in, matching the three things the corpus actually asks for:

* ``ids=us/us/576/644`` (or a raw "576 U.S. 644") — **targeted** resolution of a
  hanging citation, via ``POST /citation-lookup/``. This is the path the harvest
  worklist and ⌘K drive, and the reason the adapter exists.
* ``cluster_ids=2812209`` — a case by its CourtListener id (what a courtlistener.com
  opinion URL embeds; note the URL carries the *cluster* id, not the opinion id).
* ``courts=scotus,ca9`` — **incremental** discovery: clusters ordered by
  ``date_modified``, cursor-paginated, watermarked. Keeps a bulk-seeded corpus
  current between quarterly drops. Not a backfill: ``max_pages`` bounds it and a
  bare backfill run yields the same recent-first page order.

Identity is the citation, never the CourtListener id: a case with a reporter
citation is stored as ``us/us/576/644``, and only a decision with no citation at all
(unpublished dispositions, mostly) falls back to a ``us-case/cl-<cluster>``
surrogate — flagged as such, since a surrogate is not citation-addressable.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..citations.us_cases import reporter_name, us_candidate_id
from ..core.adapter import BaseAdapter
from ..core.api_budget import BudgetExhausted, RequestBudget, Window
from ..core.errors import FetchError, RateLimitException
from ..core.http import RateLimitedClient
from ..core.models import DocType, ExtractedVia, Record, Segment, Stub

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
TOKEN_ENV = "RAGLEX_COURTLISTENER_TOKEN"

# The free tier's three concurrent rolling windows, as documented. Every one is
# overridable in settings: a Free Law Project membership (academic, commercial
# partnership) raises all three, and an operator who has one should not be held to
# the free numbers — see `configured_windows`.
FREE_TIER_WINDOWS = (
    Window("minute", 60.0, 5),
    Window("hour", 3600.0, 50),
    Window("day", 86_400.0, 125),
)

# Budget overrides, per window. Blank → the free tier's number for that window; the
# three are independent, so raising only the daily cap is a valid configuration.
LIMIT_ENV = {"minute": "RAGLEX_COURTLISTENER_PER_MINUTE",
             "hour": "RAGLEX_COURTLISTENER_PER_HOUR",
             "day": "RAGLEX_COURTLISTENER_PER_DAY"}

# Pacing floor, derived from the per-minute allowance rather than fixed: at the free
# tier's 5/minute that is 12s, and on a raised account it opens up automatically. A
# hard-coded floor would silently throttle a membership to free-tier throughput no
# matter what the settings said. Pacing can only respect the *minute* window — the
# hourly and daily ceilings are the ledger's job.
_MIN_INTERVAL_FLOOR = 0.5


def min_interval_for(windows: tuple[Window, ...]) -> float:
    """Seconds between requests that keeps the per-minute window from ever tripping."""
    per_minute = next((w for w in windows if w.name == "minute"), None)
    if not per_minute or per_minute.limit <= 0:
        return 12.0
    return max(_MIN_INTERVAL_FLOOR, per_minute.seconds / per_minute.limit)

# How much of the daily quota an unattended background drain may spend, leaving the
# rest for interactive lookups. A queue that quietly eats all 125 requests overnight
# means the one case someone actually asks for at 10am can't be fetched — so the
# backlog drip is a *reservation*, not the whole budget.
DEFAULT_QUEUE_RESERVE = 0.6


# "this window doesn't bind for my account" — a membership may lift the daily cap
# entirely while still capping per-minute/per-hour. Expressed as a limit high enough
# that it can never be the binding window, rather than as a special case threaded
# through the ledger.
_UNLIMITED = 10**9
_UNLIMITED_WORDS = {"0", "none", "unlimited", "off", "-1"}


def configured_windows() -> tuple[Window, ...]:
    """The rate-limit windows in force — the free tier unless overridden in settings.

    Each window is read independently, so a membership that raises the minute and hour
    ceilings but says nothing about a daily one is configured by setting just those
    two. A window set to 0/none/unlimited stops binding altogether.

    Limits are never validated against the account: setting a number the account
    doesn't have converts the ledger's clean refusals back into real 429s, which the
    HTTP client still handles — it just stops being predictable. Set what CourtListener
    actually granted you.
    """
    out = []
    for w in FREE_TIER_WINDOWS:
        raw = os.environ.get(LIMIT_ENV[w.name], "").strip().lower()
        if raw in _UNLIMITED_WORDS and raw != "":
            out.append(Window(w.name, w.seconds, _UNLIMITED))
            continue
        try:
            limit = int(raw) if raw else w.limit
        except ValueError:
            limit = w.limit     # unparseable → the safe (free-tier) number
        out.append(Window(w.name, w.seconds, max(1, limit)))
    return tuple(out)


def queue_reserve() -> float:
    """Fraction of the daily quota the unattended backlog queue may spend (0–1)."""
    raw = os.environ.get("RAGLEX_COURTLISTENER_QUEUE_RESERVE", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_QUEUE_RESERVE
    except ValueError:
        value = DEFAULT_QUEUE_RESERVE
    return min(1.0, max(0.0, value))


def budget_path() -> str | None:
    """Where the ledger lives — beside the catalogue, so it is as durable as the
    corpus and shared by every process that spends the same token's quota."""
    data_dir = os.environ.get("RAGLEX_DATA_DIR", "data")
    return str(Path(data_dir).expanduser() / "api_budget.sqlite")

# The seed set from the integration plan: SCOTUS + the federal circuits. Not a closed
# list — any CourtListener court id works — but it is what `courts` defaults to, and
# phase 1 of a rollout is `courts=scotus` alone.
FEDERAL_APPELLATE = (
    "scotus", "ca1", "ca2", "ca3", "ca4", "ca5", "ca6", "ca7", "ca8", "ca9", "ca10",
    "ca11", "cadc", "cafc",
)

# Which parallel citation becomes the document's identity. Official reporters first:
# a case is cited as "576 U.S. 644" far more often than by its S. Ct. or L. Ed.
# parallel, so keying on U.S. puts the node where most edges already point. Anything
# unlisted sorts last but still beats a surrogate id.
_REPORTER_PRIORITY = (
    "us", "sct", "led2d", "led",                    # Supreme Court
    "f4th", "f3d", "f2d", "f",                      # federal appellate
    "fsupp3d", "fsupp2d", "fsupp", "fedappx",       # federal trial / unreported
)

# CourtListener's opinion `type` values are number-prefixed so that sorting the raw
# string also sorts by precedential weight ("010combined" < "020lead" < "040dissent").
# Keep that order when concatenating a cluster's opinions into one document.
_OPINION_TYPE_LABELS = {
    "010combined": "Opinion", "015unamimous": "Unanimous Opinion",
    "020lead": "Opinion of the Court", "025plurality": "Plurality Opinion",
    "030concurrence": "Concurrence", "035concurrenceinpart": "Concurrence in Part",
    "040dissent": "Dissent", "050addendum": "Addendum",
    "060remittitur": "Remittitur", "070rehearing": "On Rehearing",
    "080onthemerits": "On the Merits", "090onmotiontostrike": "On Motion to Strike",
}

# "576 U.S. 644" as a user might type it into a targeted-harvest box, so `ids` accepts
# the citation as written as well as the slug the extractor mints from it.
_RAW_CITE_RE = re.compile(r"^\s*(?P<vol>\d{1,4})\s+(?P<rep>.+?)\s+(?P<page>\d{1,5})\s*$")


class CourtListenerAdapter(BaseAdapter):
    """CourtListener v4 — US case law by citation, by cluster id, or incrementally.

    ``token`` (or ``RAGLEX_COURTLISTENER_TOKEN``) is required: v4 rejects anonymous
    requests with a 401. Without one the adapter degrades cleanly — discovery yields
    nothing and says why — rather than hammering a wall.
    """

    source = "us-caselaw"
    # Class default, for the orchestrator reading the contract before construction;
    # the instance recomputes it from the configured per-minute allowance.
    min_interval = 12.0
    requires_js = False
    requires_proxy = False

    def __init__(self, *, token: str | None = None,
                 ids: str | tuple[str, ...] | None = None,
                 cluster_ids: str | tuple[str, ...] | None = None,
                 courts: str | tuple[str, ...] | None = None,
                 client: RateLimitedClient | None = None,
                 base_url: str = BASE_URL,
                 budget: RequestBudget | None = None,
                 prefer_html: bool = False) -> None:
        self.token = token or os.environ.get(TOKEN_ENV) or None
        self.base_url = base_url.rstrip("/")
        # One ledger per source key, persisted — see core/api_budget.py. Injectable so
        # tests can drive an in-memory budget with a fake clock.
        windows = configured_windows()
        self.budget = budget or RequestBudget(self.source, windows, path=budget_path())
        # Pace to whatever the per-minute allowance actually is (12s on the free tier,
        # faster on a membership) rather than to a fixed free-tier number.
        self.min_interval = min_interval_for(self.budget.windows or windows)
        self.ids = _listify(ids)
        self.cluster_ids = _listify(cluster_ids)
        self.courts = _listify(courts) or list(FEDERAL_APPELLATE)
        # An opinion carries up to seven text representations; pulling them all would
        # balloon every response. plain_text is what this corpus's consumers want
        # (citation extraction, search, embedding), so it is the default and
        # html_with_citations — CourtListener's hyperlinked display rendering — is
        # opt-in for a reading pane.
        self.prefer_html = prefer_html
        self._client = client or RateLimitedClient(
            self.source, min_interval=self.min_interval, timeout=120)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    # -- HTTP ---------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        # The literal word "Token" must precede the key — the commonest auth mistake
        # against this API, and it fails as a bare 401 with no hint.
        return {"Authorization": f"Token {self.token or ''}",
                "Accept": "application/json"}

    def _charge(self, n: int = 1) -> None:
        """Charge ``n`` requests to the quota before making them.

        Translated to ``RateLimitException`` so it travels the path the codebase
        already has for "the source is pushing back": the harvest drain stops the
        batch and leaves the remaining queue uncooled, which is exactly right — those
        references aren't dead, we just can't afford them until the window rolls.
        """
        try:
            self.budget.spend(n)
        except BudgetExhausted as exc:
            raise RateLimitException(self.source, retry_after=exc.retry_after) from exc

    def _get(self, url: str, params: dict | None = None) -> dict:
        self._charge()
        resp = self._client.get(url, params=params, headers=self._headers())
        return resp.json()

    def _post(self, url: str, data: dict) -> list[dict]:
        """POST for /citation-lookup/. 4xx is not raised here: this endpoint answers a
        *malformed or unknown* citation with a per-citation status inside a 200 body,
        and we want that detail rather than a bare FetchError."""
        self._charge()
        resp = self._client.request("POST", url, data=data, headers=self._headers(),
                                    raise_for_4xx=False)
        if resp.status_code >= 400:
            raise FetchError(f"{self.source}: HTTP {resp.status_code} from citation-lookup",
                             transient=resp.status_code >= 500)
        body = resp.json()
        return body if isinstance(body, list) else []

    # -- discover -----------------------------------------------------------
    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        if not self.configured:
            raise FetchError(
                f"{self.source}: no API token — set {TOKEN_ENV} (CourtListener v4 "
                "rejects anonymous requests). Get one at courtlistener.com/profile/api-token/",
                transient=False)
        if self.ids:
            yield from self._discover_by_citation()
        elif self.cluster_ids:
            for cid in self.cluster_ids:
                cid = str(cid).strip()
                if cid.isdigit():
                    yield Stub(stable_id=f"us-case/cl-{cid}",
                               landing_url=f"https://www.courtlistener.com/opinion/{cid}/",
                               hints={"cluster_id": cid})
        else:
            yield from self._discover_incremental(since, max_pages=max_pages)

    def _discover_by_citation(self) -> Iterator[Stub]:
        """Resolve each requested citation to a cluster via POST /citation-lookup/.

        One request per citation keeps the mapping citation→case unambiguous, but the
        endpoint also accepts a text blob and resolves every citation in it — that is
        what ``lookup_text`` is for, and it is the far cheaper call when a caller has
        many citations at once (60 valid citations/minute, 250 per request).
        """
        for want in self.ids:
            parsed = _parse_citation_ref(want)
            if not parsed:
                continue
            volume, reporter, page = parsed
            try:
                results = self._post(f"{self.base_url}/citation-lookup/",
                                     {"volume": volume, "reporter": reporter, "page": page})
            except FetchError as exc:
                if exc.transient:
                    raise
                continue
            for item in results:
                yield from self._stubs_from_lookup(item)

    def _stubs_from_lookup(self, item: dict) -> Iterator[Stub]:
        """Stubs for one citation-lookup result, branching on its per-citation status.

        200 resolved · 300 valid but ambiguous · 404 valid, not held by CourtListener ·
        400 unknown reporter (malformed, or an LLM hallucination) · 429 past the
        250-per-request cap. Only 200 and 300 carry clusters. An ambiguous 300 is NOT
        auto-imported: it matched several real cases, and picking one silently would
        attach the citing edges to the wrong authority — the caller must disambiguate.
        """
        status = item.get("status")
        if status != 200:
            return
        for cluster in item.get("clusters") or []:
            stub = _stub_for_cluster(cluster, cited_as=item.get("citation"))
            if stub:
                yield stub

    def _discover_incremental(self, since: str | None, *,
                              max_pages: int | None = None) -> Iterator[Stub]:
        """Clusters per court, ordered by ``date_modified``, cursor-paginated.

        The watermark is ``date_modified`` (not ``date_filed``): a *correction* to an
        old case has to come through too, and only the modified timestamp moves when
        it does. ``id`` is appended to the ordering because ``date_modified`` is not
        unique and ordering on a non-unique field alone makes pagination
        non-deterministic — rows can be skipped or repeated at page boundaries.
        """
        for court in self.courts:
            params = {
                "docket__court": court,
                "order_by": "date_modified,id",
                # Field selection is not an optimisation here so much as a
                # requirement: a bare cluster response carries every field, and with
                # 125 requests/day we cannot pay for what we don't store.
                "fields": "id,case_name,case_name_full,date_filed,date_modified,"
                          "citations,sub_opinions,docket,precedential_status,judges,"
                          "citation_count,absolute_url",
            }
            if since:
                params["date_modified__gte"] = since
            url = f"{self.base_url}/clusters/"
            pages = 0
            while url:
                page = self._get(url, params)
                params = None       # the `next` cursor already carries them
                for cluster in page.get("results") or []:
                    stub = _stub_for_cluster(cluster)
                    if stub:
                        yield stub
                pages += 1
                if max_pages is not None and pages >= max_pages:
                    break
                url = page.get("next")

    # -- fetch --------------------------------------------------------------
    def fetch(self, stub: Stub) -> Record | None:
        cluster = stub.hints.get("cluster")
        if cluster is None:
            cluster_id = stub.hints.get("cluster_id")
            if not cluster_id:
                return None
            try:
                cluster = self._get(f"{self.base_url}/clusters/{cluster_id}/")
            except FetchError as exc:
                if exc.transient:
                    raise       # a 5xx says nothing about whether the case exists
                return None
        return self._record(cluster, stub)

    def _record(self, cluster: dict, stub: Stub) -> Record | None:
        opinions = self._opinions(cluster)
        text, segments = _assemble_text(opinions)
        if not text.strip():
            return None         # metadata-only cluster: no opinion text to hold

        citations = _citation_slugs(cluster.get("citations") or [])
        stable_id = stub.stable_id
        # Every parallel citation that isn't the identity becomes an alias, so
        # "117 S. Ct. 905" and "519 U.S. 452" land on the same node. Without this a
        # case held under its U.S. cite leaves every S. Ct. citation of it pending
        # forever, which is exactly the failure the US matcher was built to surface.
        aliases = [slug for slug in citations if slug != stable_id]

        decided = _as_date(cluster.get("date_filed"))
        case_name = (cluster.get("case_name") or cluster.get("case_name_full")
                     or stub.title or stable_id)
        court = _court_id(cluster) or stub.court

        extra = {
            "jurisdiction": "us",
            "court_code": court,
            "cluster_id": cluster.get("id"),
            "courtlistener_url": stub.landing_url,
            "citations": [_pretty_citation(c) for c in (cluster.get("citations") or [])] or None,
            "aliases": aliases or None,
            "precedential_status": cluster.get("precedential_status"),
            "judges": cluster.get("judges") or None,
            # CourtListener's own count of citing opinions — an authority signal that
            # is cheap here and expensive to derive from a partial local corpus.
            "citation_count": cluster.get("citation_count"),
            "opinion_count": len(opinions) or None,
            "case_name_full": cluster.get("case_name_full") or None,
            "date_modified": cluster.get("date_modified"),
            # CourtListener aggregates court-supplied text; authoritative for most
            # purposes but not the court's own publication of record.
            "is_authoritative": False,
            "provider": "CourtListener (Free Law Project)",
            "upstream_license": "public domain (US government works)",
            # A decision with no reporter citation is held under a CourtListener-id
            # surrogate: it is NOT citation-addressable, so say so rather than
            # implying a citation exists that would resolve to it.
            "surrogate_id": stable_id.startswith("us-case/"),
        }
        return Record(
            source=self.source,
            stable_id=stable_id,
            doc_type=DocType.JUDGMENT,
            title=case_name,
            court=court,
            decision_date=decided,
            language="en", source_language="en",
            landing_url=stub.landing_url,
            text=text,
            segments=segments,
            extracted_via=ExtractedVia.STRUCTURED,
            extra={k: v for k, v in extra.items() if v not in (None, "", [])},
        )

    def _opinions(self, cluster: dict) -> list[dict]:
        """The cluster's opinions, in precedential order.

        One request each — the sub_opinions are URLs, not inlined — so a cluster with
        a majority, a concurrence and two dissents costs four of the day's 125. Field
        selection keeps each response to the one text representation we store.
        """
        # plain_text is always requested (the extractor and embedder consume it); the
        # HTML rendering rides along only when asked for, since it roughly doubles the
        # payload and we would otherwise be paying for a reading pane nobody opened.
        wanted = ["id", "type", "author_str", "ordering_key", "plain_text"]
        if self.prefer_html:
            wanted.append("html_with_citations")
        params = {"fields": ",".join(wanted)}
        out: list[dict] = []
        for url in cluster.get("sub_opinions") or []:
            try:
                out.append(self._get(url, params))
            except FetchError as exc:
                if exc.transient:
                    raise
                continue        # one missing opinion must not lose the whole case
        # ordering_key is only populated for Harvard/Columbia-sourced opinions, so it
        # cannot be the primary sort; the number-prefixed `type` always works.
        out.sort(key=lambda o: (o.get("ordering_key") if o.get("ordering_key") is not None
                                else 99, str(o.get("type") or "")))
        return out

    # -- public helpers ------------------------------------------------------
    def lookup_text(self, text: str) -> list[dict]:
        """Resolve every citation in a block of prose in ONE request.

        The efficient shape of this API: 250 citations matched per request against a
        64,000-character body, versus one request per citation. Callers with a
        document's worth of pending US citations should batch through here.

        Results are returned raw (one entry per citation found, each with its own
        ``status``) so the caller can act on the distinction between "not in
        CourtListener" (404) and "not a real citation" (400) — the second being the
        signal that matters if the text came from an LLM.
        """
        if not self.configured:
            raise FetchError(f"{self.source}: no API token — set {TOKEN_ENV}", transient=False)
        # Over-long bodies are rejected outright; truncate on a whitespace boundary so
        # a citation isn't split in half at the cut (which would silently mis-parse).
        body = text[:64_000]
        if len(text) > 64_000:
            body = body.rsplit(" ", 1)[0]
        return self._post(f"{self.base_url}/citation-lookup/", {"text": body})

    def budget_status(self) -> dict:
        """Live quota state — what the settings/dashboard panel shows the operator.

        Reports the ledger's own counts rather than an estimate. ``queue_allowance``
        is the separate, smaller number that governs the unattended backlog drip: the
        share of the day's requests background work may spend, so interactive lookups
        still have room at 10am after a queue ran all night.
        """
        state = self.budget.state()
        day_used, day_limit = state.windows.get("day", (0, 0))
        return {
            "configured": self.configured,
            "allowed_now": state.allowed,
            "blocked_by": state.blocked_by,
            "retry_after_seconds": round(state.retry_after),
            "remaining": state.remaining,
            "windows": {name: {"used": used, "limit": limit}
                        for name, (used, limit) in state.windows.items()},
            "queue_allowance": max(0, int(day_limit * queue_reserve()) - day_used),
            "tier": "free" if configured_windows() == FREE_TIER_WINDOWS else "custom",
        }


# -- cluster → stub/identity ------------------------------------------------
def _stub_for_cluster(cluster: dict, *, cited_as: str | None = None) -> Stub | None:
    """A discovery stub keyed by the cluster's best reporter citation.

    The whole cluster object is carried through in ``hints`` because both discovery
    paths already hold it in full — re-fetching it in ``fetch`` would double the
    request cost of every case for no new information.
    """
    cluster_id = cluster.get("id")
    if cluster_id is None:
        return None
    slugs = _citation_slugs(cluster.get("citations") or [])
    stable_id = slugs[0] if slugs else f"us-case/cl-{cluster_id}"
    absolute = cluster.get("absolute_url") or f"/opinion/{cluster_id}/"
    return Stub(
        stable_id=stable_id,
        title=cluster.get("case_name") or cluster.get("case_name_full"),
        court=_court_id(cluster),
        landing_url=f"https://www.courtlistener.com{absolute}",
        hint_date=_as_date(cluster.get("date_filed")),
        hints={"cluster": cluster, "cluster_id": str(cluster_id),
               "watermark": cluster.get("date_modified"),
               **({"cited_as": cited_as} if cited_as else {})},
    )


def _citation_slugs(citations: list[dict]) -> list[str]:
    """The cluster's parallel citations as ``us/<rep>/<vol>/<page>`` slugs, best first.

    "Best" is the reporter the case is most often cited by (see _REPORTER_PRIORITY):
    the head of this list becomes the document's identity, the tail become aliases, so
    the ordering decides which node the majority of citing edges land on directly.
    """
    seen: dict[str, None] = {}
    for cite in citations:
        volume, reporter, page = cite.get("volume"), cite.get("reporter"), cite.get("page")
        if volume is None or page is None or not reporter:
            continue
        seen.setdefault(us_candidate_id(volume, reporter, page))
    def rank(slug: str) -> tuple[int, str]:
        rep = slug.split("/")[1] if "/" in slug else ""
        return (_REPORTER_PRIORITY.index(rep) if rep in _REPORTER_PRIORITY
                else len(_REPORTER_PRIORITY), slug)
    return sorted(seen, key=rank)


def _court_id(cluster: dict) -> str | None:
    """The court id, from wherever this response shape carries it.

    A cluster references its docket by URL, so the court is usually only present when
    the caller asked for it (citation-lookup inlines more than the list endpoint) —
    hence the several fallbacks rather than one field read.
    """
    for key in ("court_id", "court"):
        value = cluster.get(key)
        if isinstance(value, str) and "/" not in value:
            return value
    docket = cluster.get("docket")
    if isinstance(docket, dict):
        court = docket.get("court_id") or docket.get("court")
        if isinstance(court, str) and "/" not in court:
            return court
    # ".../courts/scotus/" → "scotus"
    for value in (cluster.get("court"), cluster.get("docket")):
        if isinstance(value, str) and "/courts/" in value:
            return value.rstrip("/").rsplit("/", 1)[-1]
    return None


# -- text assembly ----------------------------------------------------------
def _assemble_text(opinions: list[dict]) -> tuple[str, list[Segment]]:
    """Concatenate a cluster's opinions into one document, one segment each.

    A cluster is the citable unit — "576 U.S. 644" names the decision, not the
    majority opinion alone — so the dissents and concurrences belong in the same
    document. Each becomes a labelled segment ("Dissent — Scalia") so the chunker
    keeps them apart and a pinpoint can still land in the right opinion.
    """
    parts: list[str] = []
    segments: list[Segment] = []
    cursor = 0
    for op in opinions:
        body = _opinion_text(op)
        if not body.strip():
            continue
        label = _OPINION_TYPE_LABELS.get(str(op.get("type") or ""), "Opinion")
        author = (op.get("author_str") or "").strip()
        if author:
            label = f"{label} — {author}"
        header = f"{label}\n\n"
        chunk = header + body.strip() + "\n\n"
        parts.append(chunk)
        segments.append(Segment(label=label, char_start=cursor,
                                char_end=cursor + len(chunk), kind="zone"))
        cursor += len(chunk)
    return "".join(parts), segments


def _opinion_text(opinion: dict) -> str:
    """One opinion's text, preferring the cleanest representation available.

    CourtListener populates these fields according to how it ingested the decision, so
    no single one is always present; try them in order of how much structure survives.
    html_with_citations is the display rendering with internal citation links, and
    plain_text is what the extractor wants — both are stripped to text here, but the
    HTML form keeps paragraph breaks a PDF-derived plain_text sometimes loses.
    """
    for key in ("plain_text", "html_with_citations", "html", "html_columbia",
                "html_lawbox", "html_anon_2020", "xml_harvard"):
        value = opinion.get(key)
        if value and str(value).strip():
            return _strip_markup(str(value)) if key != "plain_text" else str(value)
    return ""


def _strip_markup(markup: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


# -- parsing helpers --------------------------------------------------------
def _parse_citation_ref(ref: str) -> tuple[str, str, str] | None:
    """``(volume, reporter, page)`` from either the slug the extractor mints
    (``us/us/576/644``) or the citation as a human writes it ("576 U.S. 644").

    The slug's reporter token is *canonicalised* ("sct"), which the API will not
    recognise, so it is expanded back to a real abbreviation on the way out.
    """
    ref = (ref or "").strip()
    if ref.lower().startswith("us/"):
        parts = ref.split("/")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            return parts[2], _expand_reporter(parts[1]), parts[3]
        return None
    m = _RAW_CITE_RE.match(ref)
    if m:
        return m.group("vol"), m.group("rep").strip(), m.group("page")
    return None


def _expand_reporter(slug: str) -> str:
    """``"sct"`` → ``"S. Ct."``. The extractor's canonical slug token is not a reporter
    abbreviation and CourtListener will not recognise it, so it is expanded back before
    being sent. An unlisted slug is passed through and the API says so (status 400)."""
    return reporter_name(slug)


def _pretty_citation(cite: dict) -> str | None:
    volume, reporter, page = cite.get("volume"), cite.get("reporter"), cite.get("page")
    if volume is None or page is None or not reporter:
        return None
    return f"{volume} {reporter} {page}"


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _listify(value: str | tuple | list | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


__all__ = ["CourtListenerAdapter", "FEDERAL_APPELLATE", "TOKEN_ENV", "BASE_URL"]
