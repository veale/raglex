"""CanLII — reference parsing, database mapping, the metadata-stub adapter, the
citator edges (with the citing-cases cap), routing, and the enrichment op.

Network-free throughout: a fake HTTP client serves recorded API shapes (verified
against the live API), and the facade test drives a real sqlite catalogue in tmp_path.
"""

from __future__ import annotations

import json

import pytest

from raglex.adapters.canlii import (
    CanLIIAdapter,
    case_id_for,
    ca_slug,
    configured_windows,
    database_for,
    federal_statute_slug,
    min_interval_for,
    parse_ca_ref,
)
from raglex.citations.snowball import _classify
from raglex.core.api_budget import RequestBudget, Window
from raglex.core.errors import RateLimitException
from raglex.core.models import DocType, RelationshipType


# -- identity plumbing ------------------------------------------------------
def test_parse_ca_ref_accepts_every_written_form():
    # the slug the extractor mints, the citation as written, and CanLII's caseId all
    # collapse onto one (court, year, num) so both sides of every edge agree
    assert parse_ca_ref("scc/2011/10") == ("scc", "2011", 10)
    assert parse_ca_ref("2011 SCC 10") == ("scc", "2011", 10)
    assert parse_ca_ref("2011scc10") == ("scc", "2011", 10)
    assert parse_ca_ref("1980canlii21") == ("canlii", "1980", 21)
    assert parse_ca_ref("[2011] SCC 10") is None      # Canadian neutrals are bracketless
    assert parse_ca_ref("") is None
    # shape-only by design: the ROUTING gate (the classifier's jurisdiction check) is
    # what keeps non-Canadian slugs away from the adapter, not the parser


def test_slug_and_case_id_round_trip():
    court, year, num = parse_ca_ref("2008 SCC 9")
    assert ca_slug(court, year, num) == "scc/2008/9"
    assert case_id_for(court, year, num) == "2008scc9"


def test_database_mapping_covers_the_federal_exceptions():
    # verified against the live database list: identity for the provinces, the
    # bilingual-named federal databases overridden
    assert database_for("onca") == "onca"
    assert database_for("abkb") == "abkb"
    assert database_for("SCC") == "csc-scc"
    assert database_for("fc") == "fct"
    assert database_for("tcc") == "cci-tcc"
    assert database_for("cmac") == "cmac-cacm"


def test_federal_statute_slug_maps_rsc_chapters_only():
    # "RSC 1985, c C-46" is the ca-federal node ca/act/c-46; provincial and
    # supplement citations have no held id to land on, so they stay name-keyed
    assert federal_statute_slug("RSC 1985, c C-46") == "ca/act/c-46"
    assert federal_statute_slug("RSC 1985, c H-6") == "ca/act/h-6"
    assert federal_statute_slug("SNB 1984, c C-5.1") is None
    assert federal_statute_slug("RSC 1985, c 1 (5th Supp)") is None
    assert federal_statute_slug(None) is None


def test_canadian_neutral_citations_route_to_the_adapter():
    assert _classify("scc/2011/10", "case")[2] == "ca-canlii"
    assert _classify("skkb/2023/4", "case")[2] == "ca-canlii"
    # a bare CanLII number carries no database → NOT routable (it resolves from the
    # other side, when the citator names it)
    assert _classify("canlii/1980/21", "case")[2] is None


def test_windows_are_overridable_but_default_conservative(monkeypatch):
    for env in ("RAGLEX_CANLII_PER_MINUTE", "RAGLEX_CANLII_PER_HOUR", "RAGLEX_CANLII_PER_DAY"):
        monkeypatch.delenv(env, raising=False)
    windows = configured_windows()
    limits = {w.name: w.limit for w in windows}
    assert limits["day"] == 4000 and limits["minute"] == 20
    assert min_interval_for(windows) == pytest.approx(3.0)
    monkeypatch.setenv("RAGLEX_CANLII_PER_MINUTE", "60")
    assert min_interval_for(configured_windows()) == pytest.approx(1.0)


# -- fake API ---------------------------------------------------------------
DUNSMUIR = {
    "databaseId": "csc-scc", "caseId": "2008scc9",
    "url": "https://canlii.ca/t/1vxsm",
    "longUrl": "https://www.canlii.org/en/ca/scc/doc/2008/2008scc9/2008scc9.html",
    "title": "Dunsmuir v. New Brunswick",
    "citation": "2008 SCC 9 (CanLII), [2008] 1 SCR 190",
    "language": "en", "docketNumber": "31459", "decisionDate": "2008-03-07",
    "keywords": "Administrative law — Judicial review — Standard of review",
    "topics": "Administrative remedies — Judicial review",
    "concatenatedId": "2008csc-scc9",
}
CITED_CASES = {"citedCases": [
    {"databaseId": "csc-scc", "caseId": {"en": "2004scc28"},
     "title": "AUPE v. Lethbridge Community College",
     "citation": "2004 SCC 28 (CanLII), [2004] 1 SCR 727"},
    {"databaseId": "csc-scc", "caseId": {"en": "1980canlii21"},
     "title": "Att. Gen. of Can. v. Inuit Tapirisat et al.",
     "citation": "1980 CanLII 21 (SCC), [1980] 2 SCR 735"},
]}
CITED_LEG = {"citedLegislations": [
    {"databaseId": "nbs", "legislationId": "snb-1984-c-c-5.1",
     "title": "Civil Service Act", "citation": "SNB 1984, c C-5.1", "type": "STATUTE"},
    {"databaseId": "cas", "legislationId": "rsc-1985-c-h-6",
     "title": "Canadian Human Rights Act", "citation": "RSC 1985, c H-6", "type": "STATUTE"},
]}
CITING = {"citingCases": [
    {"databaseId": "csc-scc", "caseId": {"en": "2019scc65"},
     "title": "Canada v. Vavilov", "citation": "2019 SCC 65 (CanLII), [2019] 4 SCR 653"},
    {"databaseId": "fct", "caseId": {"en": "2008fc991"},
     "title": "Rioux v. Canada", "citation": "2008 FC 991 (CanLII)"},
]}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class _FakeClient:
    """Routes CanLII paths to canned payloads and records what was asked."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def request(self, method, url, *, params=None, raise_for_4xx=True, **kw):
        path = url.split("api.canlii.org/v1", 1)[-1]
        self.calls.append(path)
        assert params and params.get("api_key"), "every call must carry the key"
        for fragment, payload in self.routes.items():
            if fragment in path:
                return _FakeResponse(payload)
        return _FakeResponse([{"error": "MISSING"}], status_code=404)


def _adapter(routes, **kw):
    budget = RequestBudget("ca-canlii", (Window("day", 86_400.0, 1000),))
    client = _FakeClient(routes)
    a = CanLIIAdapter(key="k", client=client, budget=budget, **kw)
    return a, client


# -- targeted fetch → metadata stub ----------------------------------------
def test_targeted_fetch_builds_a_metadata_stub_with_citator_edges():
    a, client = _adapter({
        "/caseBrowse/en/csc-scc/2008scc9/": DUNSMUIR,
        "/caseCitator/en/csc-scc/2008scc9/citedCases": CITED_CASES,
        "/caseCitator/en/csc-scc/2008scc9/citedLegislations": CITED_LEG,
        "/caseCitator/en/csc-scc/2008scc9/citingCases": CITING,
    }, ids="2008 SCC 9")
    stubs = list(a.discover(None))
    assert [s.stable_id for s in stubs] == ["scc/2008/9"]
    rec = a.fetch(stubs[0])
    assert rec.doc_type == DocType.JUDGMENT
    assert rec.title == "Dunsmuir v. New Brunswick"
    assert rec.text is None                       # the API never returns judgment text
    assert rec.extra["metadata_only"] is True
    assert rec.extra["canlii_url"] == "https://canlii.ca/t/1vxsm"
    assert rec.landing_url.endswith("2008scc9.html")
    assert str(rec.decision_date) == "2008-03-07"
    # the parallel report citation resolves here (both spellings)
    assert "[2008] 1 SCR 190" in rec.extra["aliases"]
    # citator → typed edges keyed by the extractor's own slugs
    by_type = {}
    for r in rec.relations:
        by_type.setdefault(str(r.relationship_type), set()).add(r.dst_id)
    assert "scc/2004/28" in by_type["mentions"]
    assert "canlii/1980/21" in by_type["mentions"]     # pre-neutral CanLII id
    assert "ca/act/h-6" in by_type["mentions"]         # federal statute mapped to ca-federal
    assert None in by_type["mentions"]                 # provincial statute stays name-keyed
    assert by_type["cited_by"] == {"scc/2019/65", "fc/2008/991"}
    assert rec.extra["canlii_citing_count"] == 2


def test_citing_edges_are_capped_but_the_count_is_kept():
    big = {"citingCases": CITING["citingCases"] * 300}   # 600 rows
    a, _ = _adapter({
        "/caseBrowse/en/csc-scc/2008scc9/": DUNSMUIR,
        "/caseCitator/en/csc-scc/2008scc9/citedCases": {"citedCases": []},
        "/caseCitator/en/csc-scc/2008scc9/citedLegislations": {"citedLegislations": []},
        "/caseCitator/en/csc-scc/2008scc9/citingCases": big,
    }, ids="scc/2008/9", citing_cap=200)
    rec = a.fetch(next(iter(a.discover(None))))
    # a partial slice of an unordered 600-row list would mislead: count only
    assert rec.extra["canlii_citing_count"] == 600
    assert not any(str(r.relationship_type) == "cited_by" for r in rec.relations)


def test_a_bare_canlii_id_is_skipped_not_guessed():
    a, client = _adapter({}, ids="canlii/1980/21")
    assert list(a.discover(None)) == []
    assert client.calls == []       # no database → no request to guess at


def test_missing_case_is_an_absence_not_a_failure():
    from raglex.core.errors import FetchError

    a, _ = _adapter({}, ids="scc/2099/999")
    stub = next(iter(a.discover(None)))
    with pytest.raises(FetchError) as exc:
        a.fetch(stub)
    assert not exc.value.transient


def test_case_metadata_returns_none_for_a_miss():
    a, _ = _adapter({"/caseBrowse/en/csc-scc/2008scc9/": DUNSMUIR})
    assert a.case_metadata("scc/2008/9")["title"] == "Dunsmuir v. New Brunswick"
    assert a.case_metadata("onca/2099/1") is None
    assert a.case_metadata("canlii/1980/21") is None    # database-less: not lookupable


def test_budget_exhaustion_surfaces_as_rate_limiting():
    budget = RequestBudget("ca-canlii", (Window("day", 86_400.0, 1),))
    client = _FakeClient({"/caseBrowse/en/csc-scc/2008scc9/": DUNSMUIR})
    a = CanLIIAdapter(key="k", client=client, budget=budget, ids="scc/2008/9",
                      citator=False)
    stub = next(iter(a.discover(None)))
    assert a.fetch(stub) is not None          # spends the single request
    with pytest.raises(RateLimitException):
        a.fetch(stub)                         # the next is refused BEFORE the call


def test_unconfigured_adapter_says_how_to_get_a_key():
    from raglex.core.errors import FetchError

    a = CanLIIAdapter(key=None, budget=RequestBudget("ca-canlii", ()),
                      client=_FakeClient({}), ids="scc/2008/9")
    with pytest.raises(FetchError) as exc:
        list(a.discover(None))
    assert "RAGLEX_CANLII_API_KEY" in str(exc.value) and not exc.value.transient


# -- incremental discovery --------------------------------------------------
def test_incremental_discovery_pages_a_database():
    page = {"cases": [
        {"databaseId": "onca", "caseId": {"en": "2026onca530"},
         "title": "Lang-Newlands v. Newlands", "citation": "2026 ONCA 530 (CanLII)",
         "longUrl": "https://www.canlii.org/en/on/onca/doc/2026/2026onca530/2026onca530.html"},
    ]}
    a, client = _adapter({"/caseBrowse/en/onca/": page}, databases="onca", detail=False)
    stubs = list(a.discover("2026-07-01", max_pages=1))
    assert [s.stable_id for s in stubs] == ["onca/2026/530"]
    rec = a.fetch(stubs[0])
    assert rec.title == "Lang-Newlands v. Newlands"
    assert rec.extra["metadata_only"] is True
    # detail=False: the record is built from the list row — one request total
    assert client.calls == ["/caseBrowse/en/onca/"]


# -- the facade enrichment op ----------------------------------------------
def _config(tmp_path):
    from raglex.config import Config

    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite",
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json", embed_provider="local-hashing",
        embed_model=None,
    )


def test_canlii_enrich_decorates_held_docs_and_mints_aliases(tmp_path, monkeypatch):
    from raglex.core.models import ExtractedVia, Record
    from raglex.facade import Facade

    facade = Facade(_config(tmp_path))
    # a held A2AJ-style Canadian judgment, full text, not yet CanLII-checked
    rec = Record(source="ca-caselaw", stable_id="scc/2008/9", doc_type=DocType.JUDGMENT,
                 title="Dunsmuir v. New Brunswick", court="scc",
                 text="Judicial review of an adjudicator's award.",
                 extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    with facade._open() as (cat, _rs, ts):
        text_path = str(ts.put(rec.payload_hash, rec.text))
        cat.upsert_document(rec, text_path=text_path)
        cat.commit()

    a, _client = _adapter({
        "/caseBrowse/en/csc-scc/2008scc9/": DUNSMUIR,
        "/caseCitator/en/csc-scc/2008scc9/citedCases": CITED_CASES,
        "/caseCitator/en/csc-scc/2008scc9/citedLegislations": CITED_LEG,
        "/caseCitator/en/csc-scc/2008scc9/citingCases": CITING,
    })
    monkeypatch.setattr("raglex.adapters.registry.get_adapter",
                        lambda key, **kw: a if key == "ca-canlii" else None)
    out = facade.canlii_enrich(limit=10)
    assert out["enriched"] == 1 and out["rate_limited"] is False
    with facade._open() as (cat, _rs, _ts):
        meta = cat.document_meta("scc/2008/9")
        assert meta["canlii_url"] == "https://canlii.ca/t/1vxsm"
        assert meta["docket_number"] == "31459"
        assert meta["canlii_checked_at"]
        assert meta["canlii_citing_count"] == 2
        # the parallel report citation now resolves to the held node
        assert cat.get_alias("[2008] 1 scr 190".casefold()) or \
               cat.get_alias("[2008] 1 SCR 190".casefold())
        rels = cat.relations_for("scc/2008/9")
        types = {r["relationship_type"] for r in rels}
        assert "cited_by" in types
        # a second run is a no-op: everything is stamped canlii_checked_at
        again = facade.canlii_enrich(limit=10)
        assert again["checked"] == 0


def test_canlii_enrich_without_a_key_degrades_cleanly(tmp_path, monkeypatch):
    from raglex.facade import Facade

    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAGLEX_CANLII_API_KEY", raising=False)
    out = Facade(_config(tmp_path)).canlii_enrich(limit=5)
    assert "error" in out and out["enriched"] == 0
