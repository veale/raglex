"""CourtListener — the free-tier budget ledger, the API adapter, and the bulk importer.

Network-free throughout: a fake HTTP client serves the API responses and the bulk
importer reads tiny CSV fixtures written to tmp_path.
"""

from __future__ import annotations

import csv

import pytest

from raglex.adapters.courtlistener import (
    CourtListenerAdapter,
    _citation_slugs,
    _court_id,
    _parse_citation_ref,
    configured_windows,
    min_interval_for,
    queue_reserve,
)
from raglex.adapters.courtlistener_bulk import CourtListenerBulkAdapter
from raglex.citations.snowball import _classify
from raglex.citations.us_cases import us_candidate_id, us_case_citations
from raglex.core.api_budget import BudgetExhausted, RequestBudget, Window
from raglex.core.errors import RateLimitException
from raglex.core.models import DocType


# -- the budget ledger ------------------------------------------------------
class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _budget(clock, path=None, **limits):
    windows = (Window("minute", 60.0, limits.get("minute", 5)),
               Window("hour", 3600.0, limits.get("hour", 50)),
               Window("day", 86_400.0, limits.get("day", 125)))
    return RequestBudget("us-caselaw", windows, path=path, now=clock)


def test_budget_refuses_once_a_window_is_full():
    clock = _Clock()
    b = _budget(clock)
    for _ in range(5):
        b.spend()
    with pytest.raises(BudgetExhausted) as exc:
        b.spend()
    # the minute window binds first (5 < 50 < 125) and retry_after is the wait for the
    # OLDEST request to age out of it — the whole window, since all five were bursted
    assert exc.value.state.blocked_by == "minute"
    assert 59 <= exc.value.retry_after <= 60


def test_budget_windows_roll_rather_than_reset():
    clock = _Clock()
    b = _budget(clock)
    for _ in range(5):
        b.spend()
    clock.advance(61)                       # the minute window has rolled
    b.spend()                               # …so there is room again
    assert b.state().windows["minute"] == (1, 5)
    # but those six still count against the hour and the day
    assert b.state().windows["hour"] == (6, 50)
    assert b.state().windows["day"] == (6, 125)


def test_daily_cap_binds_after_the_short_windows_roll():
    clock = _Clock()
    b = _budget(clock, day=10)
    for _ in range(10):
        b.spend()
        clock.advance(61)                   # never trip the minute window
    with pytest.raises(BudgetExhausted) as exc:
        b.spend()
    assert exc.value.state.blocked_by == "day"


def test_budget_is_all_or_nothing():
    """A case costs several requests (cluster + one per opinion); a caller that can't
    afford the whole thing must not strand itself half-way."""
    clock = _Clock()
    b = _budget(clock, minute=5)
    b.spend(3)
    with pytest.raises(BudgetExhausted):
        b.spend(3)                          # only 2 left — refuse rather than part-spend
    assert b.state().windows["minute"] == (3, 5)
    b.spend(2)                              # exactly the remainder is fine


def test_a_refused_multi_spend_names_the_window_and_the_wait():
    """A window with room left is not "full", but it still blocks a bigger spend —
    and the operator has to be told which window and when, not
    "exhausted (None window), retry in 0s"."""
    clock = _Clock()
    b = _budget(clock, minute=5)
    b.spend(3)                              # 2 slots left; no window is full
    clock.advance(10)
    with pytest.raises(BudgetExhausted) as exc:
        b.spend(4)
    assert exc.value.state.blocked_by == "minute"
    # capacity for all 4 arrives when the SECOND-oldest ages out, not the first:
    # waiting on the first and retrying would still not fit, and the queue would
    # busy-loop against its own budget
    assert 49 <= exc.value.retry_after <= 50


def test_budget_persists_across_processes(tmp_path):
    """The windows are hours long and a job is minutes long: an in-process counter
    would hand back a fresh 125 on every run and blow the daily cap."""
    path = tmp_path / "budget.sqlite"
    clock = _Clock()
    first = _budget(clock, path=path)
    for _ in range(4):
        first.spend()
    first.close()

    second = _budget(clock, path=path)      # a "new process"
    assert second.state().windows["minute"] == (4, 5)
    second.spend()
    with pytest.raises(BudgetExhausted):
        second.spend()


def test_reading_the_budget_does_not_need_the_write_lock(tmp_path):
    """The dashboard polls the budget while a harvest is spending it. If reading took
    the write lock, a UI refresh mid-harvest would fail as "database is locked"."""
    path = tmp_path / "budget.sqlite"
    clock = _Clock()
    harvester = _budget(clock, path=path)      # the running job
    dashboard = _budget(clock, path=path)      # the polling UI

    # hold an open write transaction, as a spend in progress would
    harvester._conn.execute("BEGIN IMMEDIATE")
    harvester._conn.execute("INSERT INTO api_requests (source, at) VALUES (?, ?)",
                            ("us-caselaw", clock()))
    # the dashboard must still be able to read the state
    assert dashboard.state().allowed is True
    harvester._conn.commit()
    assert dashboard.state().windows["minute"][0] == 1


def test_budget_reset_clears_the_ledger(tmp_path):
    clock = _Clock()
    b = _budget(clock, path=tmp_path / "b.sqlite")
    b.spend(5)
    b.reset()
    assert b.state().windows["minute"] == (0, 5)


def test_configured_windows_default_to_the_free_tier(monkeypatch):
    monkeypatch.delenv("RAGLEX_COURTLISTENER_PER_DAY", raising=False)
    assert {w.name: w.limit for w in configured_windows()} == {
        "minute": 5, "hour": 50, "day": 125}


def test_limits_are_overridable_for_a_raised_account(monkeypatch):
    monkeypatch.setenv("RAGLEX_COURTLISTENER_PER_DAY", "5000")
    assert {w.name: w.limit for w in configured_windows()}["day"] == 5000
    # garbage falls back to the free tier rather than crashing or uncapping
    monkeypatch.setenv("RAGLEX_COURTLISTENER_PER_DAY", "lots")
    assert {w.name: w.limit for w in configured_windows()}["day"] == 125


def test_membership_limits_are_set_per_window(monkeypatch):
    """An academic/commercial membership raises the windows independently — e.g.
    20/minute and 1000/hour with nothing said about a daily cap."""
    monkeypatch.setenv("RAGLEX_COURTLISTENER_PER_MINUTE", "20")
    monkeypatch.setenv("RAGLEX_COURTLISTENER_PER_HOUR", "1000")
    monkeypatch.delenv("RAGLEX_COURTLISTENER_PER_DAY", raising=False)
    limits = {w.name: w.limit for w in configured_windows()}
    assert limits == {"minute": 20, "hour": 1000, "day": 125}


def test_a_window_can_be_turned_off_entirely(monkeypatch):
    """"0"/"none" = this window doesn't bind for my account — for a membership that
    caps per-minute and per-hour but sets no daily ceiling."""
    monkeypatch.setenv("RAGLEX_COURTLISTENER_PER_DAY", "none")
    day = next(w for w in configured_windows() if w.name == "day")
    clock = _Clock()
    b = RequestBudget("us-caselaw", (day,), now=clock)
    b.spend(10_000)
    assert b.state().allowed is True


def test_pacing_follows_the_per_minute_allowance(monkeypatch):
    """A fixed 12s floor would throttle a raised account to free-tier throughput no
    matter what the settings said."""
    monkeypatch.delenv("RAGLEX_COURTLISTENER_PER_MINUTE", raising=False)
    assert min_interval_for(configured_windows()) == 12.0        # free tier: 60/5
    monkeypatch.setenv("RAGLEX_COURTLISTENER_PER_MINUTE", "20")
    assert min_interval_for(configured_windows()) == 3.0         # membership: 60/20
    # and the adapter actually adopts it
    adapter = CourtListenerAdapter(token="t", client=_FakeClient({}))
    assert adapter.min_interval == 3.0


def test_budget_status_reports_uncapped_windows_as_null(monkeypatch):
    """The "no limit" sentinel is an implementation detail. Leaking it would render as
    "0/1000000000 requests" and a queue allowance of 600 million."""
    monkeypatch.setenv("RAGLEX_COURTLISTENER_PER_DAY", "none")
    adapter = CourtListenerAdapter(token="t", client=_FakeClient({}),
                                   budget=_budget(_Clock(), day=10**9))
    status = adapter.budget_status()
    assert status["windows"]["day"]["limit"] is None
    assert status["queue_allowance"] is None
    assert status["daily_cap"] is False
    # the capped windows still report real numbers
    assert status["windows"]["minute"]["limit"] == 5


def test_queue_reserve_is_bounded(monkeypatch):
    monkeypatch.setenv("RAGLEX_COURTLISTENER_QUEUE_RESERVE", "0.25")
    assert queue_reserve() == 0.25
    monkeypatch.setenv("RAGLEX_COURTLISTENER_QUEUE_RESERVE", "9")
    assert queue_reserve() == 1.0           # never more than the whole quota


# -- identity ---------------------------------------------------------------
def test_extractor_and_adapter_mint_the_same_slug():
    """The join key between a pending citation and the harvested case. If these two
    ever drift, every US citation stays pending forever with the case sitting right
    there in the corpus."""
    (found,) = us_case_citations("Obergefell v. Hodges, 576 U.S. 644 (2015)")
    assert found.candidate_id == us_candidate_id(576, "U.S.", 644) == "us/us/576/644"


def test_citation_slugs_rank_the_official_reporter_first():
    """The head becomes the document id and the tail become aliases, so this ordering
    decides which node most citing edges land on without an alias hop."""
    slugs = _citation_slugs([
        {"volume": "117", "reporter": "S. Ct.", "page": "905"},
        {"volume": "519", "reporter": "U.S.", "page": "452"},
        {"volume": "136", "reporter": "L. Ed. 2d", "page": "79"},
    ])
    assert slugs[0] == "us/us/519/452"
    assert set(slugs[1:]) == {"us/sct/117/905", "us/led2d/136/79"}


def test_citation_slugs_skip_incomplete_citations():
    assert _citation_slugs([{"volume": "519", "reporter": "", "page": "452"},
                            {"volume": None, "reporter": "U.S.", "page": "1"}]) == []


def test_parse_citation_ref_accepts_slug_and_written_form():
    # the slug the extractor mints — the canonical reporter token has to be expanded
    # back to a real abbreviation the API will recognise
    assert _parse_citation_ref("us/sct/135/2401") == ("135", "S. Ct.", "2401")
    # and the citation as a human types it
    assert _parse_citation_ref("576 U.S. 644") == ("576", "U.S.", "644")
    assert _parse_citation_ref("not a citation") is None


def test_court_id_read_from_whichever_shape_carries_it():
    assert _court_id({"court_id": "scotus"}) == "scotus"
    assert _court_id({"docket": {"court_id": "ca9"}}) == "ca9"
    assert _court_id({"court": "https://www.courtlistener.com/api/rest/v4/courts/ca2/"}) == "ca2"
    assert _court_id({}) is None


def test_us_citations_route_to_the_adapter():
    """Routable → the reference joins the harvest worklist and the auto-drain."""
    form, juris, adapter = _classify("us/us/576/644", "case")
    assert (juris, adapter) == ("US", "us-caselaw")


# -- the API adapter --------------------------------------------------------
class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """Serves canned API responses and records what was asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs.get("params")))
        for key, payload in self.routes.items():
            if key in url:
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected GET {url}")

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("data")))
        for key, payload in self.routes.items():
            if key in url:
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected {method} {url}")


_CLUSTER = {
    "id": 2812209,
    "case_name": "Obergefell v. Hodges",
    "case_name_full": "James Obergefell, et al. v. Richard Hodges",
    "date_filed": "2015-06-26",
    "date_modified": "2024-01-02T03:04:05Z",
    "court_id": "scotus",
    "precedential_status": "Published",
    "citation_count": 4200,
    "citations": [{"volume": "576", "reporter": "U.S.", "page": "644"},
                  {"volume": "135", "reporter": "S. Ct.", "page": "2584"}],
    "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/1/",
                     "https://www.courtlistener.com/api/rest/v4/opinions/2/"],
    "absolute_url": "/opinion/2812209/obergefell-v-hodges/",
}

_OPINIONS = {
    "opinions/1/": {"id": 1, "type": "020lead", "author_str": "Kennedy",
                    "plain_text": "The Constitution promises liberty."},
    "opinions/2/": {"id": 2, "type": "040dissent", "author_str": "Scalia",
                    "plain_text": "I write separately to note my dissent."},
}


def _adapter(routes, clock=None, **kwargs):
    clock = clock or _Clock()
    return CourtListenerAdapter(
        token="test-token", client=_FakeClient(routes),
        budget=_budget(clock, **kwargs.pop("limits", {})), **kwargs)


def test_targeted_lookup_stores_the_case_under_its_citation():
    routes = {"citation-lookup": [{"citation": "576 U.S. 644", "status": 200,
                                   "clusters": [_CLUSTER]}],
              **_OPINIONS}
    adapter = _adapter(routes, ids="us/us/576/644")
    (stub,) = list(adapter.discover(None))
    assert stub.stable_id == "us/us/576/644"

    record = adapter.fetch(stub)
    assert record.doc_type == DocType.JUDGMENT
    assert record.title == "Obergefell v. Hodges"
    assert record.court == "scotus"
    assert record.decision_date.isoformat() == "2015-06-26"
    # the parallel S. Ct. citation becomes an alias, so a "135 S. Ct. 2584" reference
    # resolves to this same node rather than staying pending forever
    assert record.extra["aliases"] == ["us/sct/135/2584"]


def test_dissents_and_concurrences_are_kept_as_labelled_segments():
    """A cluster is the citable unit — "576 U.S. 644" names the decision, not the
    majority alone — so the separate opinions belong in one document, distinguishable."""
    adapter = _adapter({"citation-lookup": [{"citation": "576 U.S. 644", "status": 200,
                                             "clusters": [_CLUSTER]}], **_OPINIONS},
                       ids="576 U.S. 644")
    (stub,) = list(adapter.discover(None))
    record = adapter.fetch(stub)
    labels = [s.label for s in record.segments]
    assert labels == ["Opinion of the Court — Kennedy", "Dissent — Scalia"]
    assert "promises liberty" in record.text and "my dissent" in record.text
    # the segment offsets must actually address the assembled text
    for seg in record.segments:
        assert record.text[seg.char_start:seg.char_end].startswith(seg.label)


def test_opinion_stored_when_plain_text_empty_but_html_present():
    """The bug that made federal harvest look dead: CAP / Columbia / Lawbox-sourced
    opinions routinely carry an EMPTY plain_text, with the text only in xml_harvard /
    html_with_citations. The adapter must both REQUEST those fields and fall back to
    them — otherwise the case is discovered, its opinions fetched 200 OK, and the cluster
    silently stored as nothing (the observed 'discovered=1 stored=0 errors=0')."""
    cluster = {**_CLUSTER,
               "sub_opinions": ["https://www.courtlistener.com/api/rest/v4/opinions/9/"]}
    opinions = {"opinions/9/": {"id": 9, "type": "020lead", "author_str": "Roberts",
                                "plain_text": "",
                                "html_with_citations": "<p>Text lives only in the HTML.</p>"}}
    routes = {"citation-lookup": [{"citation": "576 U.S. 644", "status": 200,
                                   "clusters": [cluster]}], **opinions}
    adapter = _adapter(routes, ids="us/us/576/644")
    (stub,) = list(adapter.discover(None))
    record = adapter.fetch(stub)
    assert record is not None, "empty plain_text with populated HTML must still store"
    assert "Text lives only in the HTML." in record.text


def test_requested_opinion_fields_cover_every_text_fallback():
    """Guard against the field-list / fallback-chain drift that caused the silent drop:
    _opinions must request every representation _opinion_text is prepared to read, or a
    field that would have carried the text is never fetched."""
    from raglex.adapters.courtlistener import _OPINION_TEXT_FIELDS

    adapter = _adapter({"citation-lookup": [{"citation": "576 U.S. 644", "status": 200,
                                             "clusters": [_CLUSTER]}], **_OPINIONS},
                       ids="us/us/576/644")
    (stub,) = list(adapter.discover(None))
    adapter.fetch(stub)
    opinion_gets = [c for c in adapter._client.calls
                    if c[0] == "GET" and "opinions/" in c[1]]
    assert opinion_gets, "expected per-opinion GETs"
    for _method, _url, params in opinion_gets:
        requested = set((params or {}).get("fields", "").split(","))
        assert set(_OPINION_TEXT_FIELDS) <= requested, requested


def test_unresolved_and_ambiguous_lookups_import_nothing():
    """404 (valid but not held) and 400 (unknown reporter — often a hallucination)
    carry no clusters; 300 is ambiguous and must NOT be auto-imported, because
    silently picking one of several real cases attaches the citing edges to the wrong
    authority."""
    for status in (300, 400, 404):
        payload = [{"citation": "1 H. 150", "status": status,
                    "clusters": [_CLUSTER] if status == 300 else []}]
        adapter = _adapter({"citation-lookup": payload}, ids="1 H. 150")
        assert list(adapter.discover(None)) == []


def test_a_case_with_no_reporter_citation_gets_a_flagged_surrogate_id():
    cluster = {**_CLUSTER, "citations": []}
    adapter = _adapter({"citation-lookup": [{"citation": "x", "status": 200,
                                             "clusters": [cluster]}], **_OPINIONS},
                       ids="576 U.S. 644")
    (stub,) = list(adapter.discover(None))
    assert stub.stable_id == "us-case/cl-2812209"
    record = adapter.fetch(stub)
    # not citation-addressable — say so rather than implying a citation resolves to it
    assert record.extra["surrogate_id"] is True


def test_incremental_discovery_is_watermarked_and_deterministic():
    page = {"results": [_CLUSTER], "next": None}
    adapter = _adapter({"clusters/": page}, courts="scotus")
    stubs = list(adapter.discover("2023-01-01T00:00:00Z"))
    assert [s.stable_id for s in stubs] == ["us/us/576/644"]
    assert stubs[0].hints["watermark"] == "2024-01-02T03:04:05Z"
    _method, _url, params = adapter._client.calls[0]
    assert params["date_modified__gte"] == "2023-01-01T00:00:00Z"
    # id breaks ties: ordering on a non-unique field alone makes cursor pagination
    # skip or repeat rows at page boundaries
    assert params["order_by"] == "date_modified,id"
    # field selection is mandatory at 125 requests/day, not an optimisation
    assert "fields" in params and "sub_opinions" in params["fields"]


def test_exhausted_budget_surfaces_as_rate_limiting_not_failure():
    """The distinction the harvest drain turns on: rate-limited stops the batch and
    leaves the queue intact, whereas an error would cool those references off — writing
    off perfectly good citations because we simply ran out of quota."""
    clock = _Clock()
    adapter = _adapter({"citation-lookup": [{"citation": "x", "status": 404, "clusters": []}]},
                       clock=clock, ids="576 U.S. 644", limits={"minute": 1})
    adapter.budget.spend(1)                 # quota is now gone
    with pytest.raises(RateLimitException) as exc:
        list(adapter.discover(None))
    assert exc.value.retry_after > 0
    assert adapter._client.calls == []      # refused BEFORE the request, not after a 429


def test_requests_are_charged_even_though_they_succeed():
    adapter = _adapter({"citation-lookup": [{"citation": "576 U.S. 644", "status": 200,
                                             "clusters": [_CLUSTER]}], **_OPINIONS},
                       ids="576 U.S. 644")
    (stub,) = list(adapter.discover(None))
    adapter.fetch(stub)
    # 1 lookup + 2 opinions
    assert adapter.budget.state().windows["day"][0] == 3


def test_no_token_is_a_clear_refusal_rather_than_a_401_loop(monkeypatch):
    monkeypatch.delenv("RAGLEX_COURTLISTENER_TOKEN", raising=False)
    adapter = CourtListenerAdapter(client=_FakeClient({}), budget=_budget(_Clock()))
    assert adapter.configured is False
    with pytest.raises(Exception) as exc:
        list(adapter.discover(None))
    assert "token" in str(exc.value).lower()


def test_lookup_text_truncates_on_a_word_boundary():
    """Over 64,000 characters is rejected outright; cutting mid-citation would
    silently mis-parse the citation at the boundary."""
    adapter = _adapter({"citation-lookup": []})
    adapter.lookup_text("word " * 20_000)
    _method, _url, data = adapter._client.calls[0]
    assert len(data["text"]) <= 64_000
    assert not data["text"].endswith("wor")


# -- the bulk importer ------------------------------------------------------
def _write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)


@pytest.fixture
def bulk_dir(tmp_path):
    """A miniature CourtListener bulk export: two courts, three cases, one citation."""
    _write_csv(tmp_path / "2026-03-31-dockets.csv", [
        {"id": "10", "court_id": "scotus"},
        {"id": "11", "court_id": "scotus"},
        {"id": "12", "court_id": "nysd"},        # outside the allowlist
    ], ["id", "court_id"])
    _write_csv(tmp_path / "2026-03-31-opinion-clusters.csv", [
        {"id": "100", "docket_id": "10", "case_name": "Obergefell v. Hodges",
         "date_filed": "2015-06-26", "date_modified": "2024-01-02",
         "citation_volume": "576", "citation_reporter": "U.S.", "citation_page": "644",
         "citation2_volume": "135", "citation2_reporter": "S. Ct.", "citation2_page": "2584"},
        {"id": "101", "docket_id": "11", "case_name": "Auer v. Robbins",
         "date_filed": "1997-02-19", "date_modified": "2024-01-02",
         "citation_volume": "519", "citation_reporter": "U.S.", "citation_page": "452",
         "citation2_volume": "", "citation2_reporter": "", "citation2_page": ""},
        {"id": "102", "docket_id": "12", "case_name": "Some District Case",
         "date_filed": "2001-01-01", "date_modified": "2024-01-02",
         "citation_volume": "1", "citation_reporter": "F. Supp. 2d", "citation_page": "1",
         "citation2_volume": "", "citation2_reporter": "", "citation2_page": ""},
    ], ["id", "docket_id", "case_name", "date_filed", "date_modified",
        "citation_volume", "citation_reporter", "citation_page",
        "citation2_volume", "citation2_reporter", "citation2_page"])
    _write_csv(tmp_path / "2026-03-31-opinions.csv", [
        {"id": "1000", "cluster_id": "100", "type": "020lead", "author_str": "Kennedy",
         "plain_text": "The Constitution promises liberty.", "ordering_key": "1"},
        {"id": "1001", "cluster_id": "100", "type": "040dissent", "author_str": "Scalia",
         "plain_text": "I dissent.", "ordering_key": "2"},
        {"id": "1002", "cluster_id": "101", "type": "010combined", "author_str": "",
         "plain_text": "Deference is owed.", "ordering_key": "1"},
        {"id": "1003", "cluster_id": "102", "type": "010combined", "author_str": "",
         "plain_text": "District court reasoning.", "ordering_key": "1"},
    ], ["id", "cluster_id", "type", "author_str", "plain_text", "ordering_key"])
    _write_csv(tmp_path / "2026-03-31-opinionscited.csv", [
        # Obergefell cites Auer
        {"id": "1", "depth": "3", "citing_opinion_id": "1000", "cited_opinion_id": "1002"},
    ], ["id", "depth", "citing_opinion_id", "cited_opinion_id"])
    return tmp_path


def test_bulk_import_filters_to_the_allowlisted_courts(bulk_dir):
    """The exports are whole-corpus snapshots of every US jurisdiction; without the
    filter a SCOTUS seed ingests millions of district-court rows."""
    adapter = CourtListenerBulkAdapter(path=bulk_dir, courts="scotus")
    stubs = list(adapter.discover(None))
    assert sorted(s.stable_id for s in stubs) == ["us/us/519/452", "us/us/576/644"]


def test_bulk_ids_and_aliases_match_the_api_adapter(bulk_dir):
    """Bulk-seeded and API-fetched copies of a case must be the SAME node, or an
    operator ends up reconciling two corpora."""
    adapter = CourtListenerBulkAdapter(path=bulk_dir, courts="scotus")
    records = {s.stable_id: adapter.fetch(s) for s in adapter.discover(None)}
    obergefell = records["us/us/576/644"]
    assert obergefell.extra["aliases"] == ["us/sct/135/2584"]
    assert obergefell.court == "scotus"
    assert [s.label for s in obergefell.segments] == [
        "Opinion of the Court — Kennedy", "Dissent — Scalia"]


def test_bulk_citation_map_becomes_resolvable_edges(bulk_dir):
    """Opinion-to-opinion edges lifted to the cluster level and keyed by the cited
    case's own slug, so they resolve as soon as that case is held — usually in the
    same import."""
    adapter = CourtListenerBulkAdapter(path=bulk_dir, courts="scotus")
    records = {s.stable_id: adapter.fetch(s) for s in adapter.discover(None)}
    edges = records["us/us/576/644"].relations
    assert [e.dst_id for e in edges] == ["us/us/519/452"]
    # and the cited case carries none back — the graph is directed
    assert records["us/us/519/452"].relations == []


def test_bulk_citation_map_can_be_skipped(bulk_dir):
    adapter = CourtListenerBulkAdapter(path=bulk_dir, courts="scotus", citation_map=False)
    records = [adapter.fetch(s) for s in adapter.discover(None)]
    assert all(r.relations == [] for r in records)


def test_bulk_min_year_and_watermark_filter(bulk_dir):
    recent = CourtListenerBulkAdapter(path=bulk_dir, courts="scotus", min_year=2000)
    assert [s.stable_id for s in recent.discover(None)] == ["us/us/576/644"]
    # re-pointing at the same drop imports nothing: every row is at the watermark
    again = CourtListenerBulkAdapter(path=bulk_dir, courts="scotus")
    assert list(again.discover("2024-01-02")) == []


def test_bulk_opinions_file_is_not_shadowed_by_the_clusters_file(bulk_dir):
    """"opinionclusters.csv" also contains the substring "opinion"; matching it as the
    opinions file is a silent mis-import where every case comes out textless."""
    adapter = CourtListenerBulkAdapter(path=bulk_dir, courts="scotus")
    assert "cluster" not in adapter._file("opinions").name.lower()
    assert "cluster" in adapter._file("clusters").name.lower()


def test_bulk_without_a_path_says_where_to_get_the_data(tmp_path):
    adapter = CourtListenerBulkAdapter(path=tmp_path / "missing", courts="scotus")
    assert adapter.configured is False
    with pytest.raises(FileNotFoundError) as exc:
        list(adapter.discover(None))
    assert "s3://" in str(exc.value)


def test_bulk_handles_postgres_null_markers(bulk_dir):
    """COPY writes NULL as \\N; read literally it becomes a bogus author or citation."""
    _write_csv(bulk_dir / "2026-03-31-opinions.csv", [
        {"id": "1000", "cluster_id": "100", "type": "020lead", "author_str": "\\N",
         "plain_text": "Text.", "ordering_key": "\\N"},
    ], ["id", "cluster_id", "type", "author_str", "plain_text", "ordering_key"])
    adapter = CourtListenerBulkAdapter(path=bulk_dir, courts="scotus")
    record = next(adapter.fetch(s) for s in adapter.discover(None)
                  if s.stable_id == "us/us/576/644")
    assert [s.label for s in record.segments] == ["Opinion of the Court"]
