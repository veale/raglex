"""The harvest drain's failure taxonomy (§5a/§5b).

The drain skips references that recently failed, so that a dead URL doesn't stall every
run. The bug this file exists to prevent: it used to record *every* non-success as a
90-day "this document does not exist", including timeouts, 5xx and rate-limiting. One bad
afternoon at legislation.gov.uk therefore wrote ~57,000 live references off for three
months, and "Harvest all routable" silently attempted nothing for seventeen days while
reporting success.
"""

from __future__ import annotations

import tempfile
from datetime import date

import pytest

from raglex.config import Config
from raglex.core.errors import FetchError, RateLimitException
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade
from raglex.pipeline.runner import RunStats


def _facade() -> Facade:
    import os

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def _doc(f: Facade, stable_id: str, text: str) -> None:
    with f._open() as (cat, _rs, ts):
        rec = Record(source="x", stable_id=stable_id, doc_type=DocType.JUDGMENT,
                     decision_date=date(2024, 1, 1), text=text, raw_bytes=text.encode(),
                     extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


# -- RunStats.outcome: the vocabulary the drain acts on ----------------------

@pytest.mark.parametrize("kwargs,expected", [
    ({"stored": 1}, "stored"),
    ({"deduped": 1}, "present"),
    ({"errors": 1, "errors_fatal": 1}, "absent"),
    ({"not_found": 1}, "absent"),
    ({"errors": 1, "errors_transient": 1}, "transient"),
    ({"rate_limited": True}, "rate_limited"),
    # a transient failure alongside a fatal one is still "we couldn't tell"
    ({"errors": 2, "errors_fatal": 1, "errors_transient": 1}, "transient"),
    # rate-limiting outranks everything: nothing else in the batch means anything
    ({"rate_limited": True, "errors_fatal": 3}, "rate_limited"),
])
def test_run_outcome_classifies_failures(kwargs, expected):
    assert RunStats(source="s", **kwargs).outcome == expected


# -- the HTTP client's transient/fatal split ---------------------------------

class _Resp:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content
        self.headers: dict = {}


def _client(status: int, *, max_retries: int = 0):
    from raglex.core.http import RateLimitedClient

    class _Inner:
        def request(self, method, url, **kw):
            return _Resp(status)

    return RateLimitedClient("s", min_interval=0, max_retries=max_retries,
                             client=_Inner(), sleep=lambda *_: None)


def test_404_is_fatal_but_500_is_transient():
    # A 404 means the document does not exist; a 500 says nothing about it at all.
    # Cooling a 500 off for three months is how a worklist dies.
    with pytest.raises(FetchError) as absent:
        _client(404).get("http://x")
    assert absent.value.transient is False

    with pytest.raises(FetchError) as unreachable:
        _client(500).get("http://x")
    assert unreachable.value.transient is True


def test_429_still_raises_rate_limit():
    with pytest.raises(RateLimitException):
        _client(429).get("http://x")


# -- the drain records the right cool-down, and stops when throttled ----------

def _hanging_ref(f: Facade) -> None:
    _doc(f, "case-1", "applying Article 17 of Regulation (EU) 2016/679")
    f.extract_citations(stable_id="case-1")


def _drain_with(f: Facade, monkeypatch, outcome: str) -> dict:
    def fake_fetch(self, cat, rs, ts, *, ref, candidate):
        return {"candidate": candidate, "adapter": "eu-legislation", "stored": 0,
                "outcome": outcome, "error": outcome}
    monkeypatch.setattr(Facade, "_fetch_reference", fake_fetch)
    return f.harvest_all_references(limit=10)


def test_absent_reference_gets_the_long_cooldown(monkeypatch):
    f = _facade()
    _hanging_ref(f)
    res = _drain_with(f, monkeypatch, "absent")
    assert res["attempted"] == 1 and res["absent"] == 1 and res["retry_later"] == 0
    with f._open() as (cat, _rs, _ts):
        assert cat.enrichment_misses("harvest-miss", max_age_days=90)
        assert not cat.enrichment_misses("harvest-retry", max_age_days=90)


def test_unreachable_reference_gets_the_short_cooldown_only(monkeypatch):
    f = _facade()
    _hanging_ref(f)
    res = _drain_with(f, monkeypatch, "transient")
    assert res["retry_later"] == 1 and res["absent"] == 0
    with f._open() as (cat, _rs, _ts):
        # never written off as absent — only onto the short (hours) retry list, whose
        # 6h default window is a fraction of the 90d absent one, so a re-drain tomorrow
        # retries it while a true 404 would still be cooling off
        assert not cat.enrichment_misses("harvest-miss", max_age_days=90)
        assert cat.enrichment_misses("harvest-retry", max_age_days=1)


def test_rate_limiting_stops_the_batch_without_recording_a_miss(monkeypatch):
    f = _facade()
    _doc(f, "case-1", "Article 17 of Regulation (EU) 2016/679 and Article 5 of Directive 95/46/EC")
    f.extract_citations(stable_id="case-1")
    seen: list[str] = []

    def fake_fetch(self, cat, rs, ts, *, ref, candidate):
        seen.append(candidate)
        return {"candidate": candidate, "outcome": "rate_limited", "stored": 0}
    monkeypatch.setattr(Facade, "_fetch_reference", fake_fetch)

    res = f.harvest_all_references(limit=10)
    assert res["rate_limited"] is True
    assert len(seen) == 1  # stopped at the first throttled fetch, didn't grind the rest
    with f._open() as (cat, _rs, _ts):
        # a throttled reference says nothing about whether the document exists
        assert not cat.enrichment_misses("harvest-miss", max_age_days=90)


def test_coverage_reports_what_is_cooling_off(monkeypatch):
    f = _facade()
    _hanging_ref(f)
    _drain_with(f, monkeypatch, "absent")
    # coverage() serves a "warming" placeholder on the first cold call and computes in the
    # background; the numbers are produced by _coverage_uncached, which is what we assert on.
    cov = f._coverage_uncached()
    assert cov["routable_references"] == 1
    assert cov["ready_references"] == 0        # nothing a drain could attempt right now
    assert cov["cooling_off"] == 1             # …and the UI can say why
    assert cov["cooling_off_absent"] == 1


def test_retry_cooled_reattempts_parked_references(monkeypatch):
    """The Corpus Map's 'harvest ALL (incl. cooling)' button: a normal drain skips a
    reference on its cool-down, but retry_cooled re-attempts it — for when the source was
    merely unavailable and the item was wrongly parked."""
    f = _facade()
    _hanging_ref(f)
    _drain_with(f, monkeypatch, "absent")      # park it on the 90-day miss list

    attempts: list[str] = []

    def fake_fetch(self, cat, rs, ts, *, ref, candidate):
        attempts.append(candidate)
        return {"candidate": candidate, "outcome": "absent", "stored": 0, "error": "absent"}
    monkeypatch.setattr(Facade, "_fetch_reference", fake_fetch)

    res = f.harvest_all_references(limit=10)                    # normal: skips the cooled ref
    assert res["attempted"] == 0 and res["skipped_recent_fail"] == 1 and attempts == []

    res2 = f.harvest_all_references(limit=10, retry_cooled=True)  # ignores the cool-down
    assert res2["attempted"] == 1 and res2["skipped_recent_fail"] == 0 and len(attempts) == 1


def test_corpus_map_splits_pending_from_cooling(monkeypatch):
    """A pending routable reference reads as 'pending' (untried, one click away) until the
    drain tries and parks it, after which it reads as 'cooling' — so the Corpus Map
    distinguishes 'never tried' from 'tried, waiting out a cool-down'."""
    f = _facade()
    _hanging_ref(f)
    before = f._corpus_map_uncached()["totals"]
    assert before["pending"] == 1 and before["cooling"] == 0

    _drain_with(f, monkeypatch, "absent")      # park it
    after = f._corpus_map_uncached()["totals"]
    assert after["pending"] == 0 and after["cooling"] == 1


def test_retry_failed_clears_the_cooldown(monkeypatch):
    f = _facade()
    _hanging_ref(f)
    _drain_with(f, monkeypatch, "absent")
    assert f._coverage_uncached()["ready_references"] == 0
    f.retry_failed_references()
    assert f._coverage_uncached()["ready_references"] == 1


def test_joined_case_already_held_mints_alias_without_refetch(monkeypatch):
    """A citation of a joined case (C-48/93) whose LEAD judgment (61993CJ0046) is
    already in the corpus: the targeted fetch must alias the joined CELEX to the held
    document and report 'present' — never refetch, never record an absence."""
    f = _facade()
    _doc(f, "ECLI:EU:C:1996:79", "Brasserie du Pecheur / Factortame judgment text")
    with f._open() as (cat, _rs, _ts):
        cat.put_alias("61993cj0046", "ECLI:EU:C:1996:79", source="celex-ecli")

    import raglex.facade as fmod
    from raglex.adapters.eu_cellar import CJEUCaseAdapter

    # the builder resolves the guess to the lead CELEX (mocked: no network)
    monkeypatch.setitem(
        fmod._TARGETED_HARVEST, "eu-cellar",
        lambda cand: CJEUCaseAdapter("61993CJ0046", celex_aliases=(cand,)))

    with f._open() as (cat, rs, ts):
        res = f._fetch_reference(cat, rs, ts, ref="C-48/93", candidate="61993CJ0048")
        assert res["outcome"] == "present"
        assert res["aliased_to"] == "ECLI:EU:C:1996:79"
        # the joined number now resolves to the held judgment
        assert cat.find_document_id("61993cj0048") == "ECLI:EU:C:1996:79"
