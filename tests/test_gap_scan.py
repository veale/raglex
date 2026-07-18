"""Neutral-citation gap-fill: probe a court/year's numbering, pull what exists, record the
gaps — historic (past-year) gaps permanently, current-year gaps for re-probe."""

import datetime

import pytest

from raglex.config import Config
from raglex.facade import Facade


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json", embed_provider="local-hashing", embed_model=None,
    )


def _stub_network(f, monkeypatch, present):
    """Make the single-item fetch resolve from a fixed 'present' set, no network."""
    def fake_fetch(cat, rs, ts, *, ref, candidate):
        ok = candidate in present
        return {"candidate": candidate, "outcome": "stored" if ok else "absent", "stored": int(ok)}
    monkeypatch.setattr(f, "_fetch_reference", fake_fetch)
    monkeypatch.setattr(f, "_extract_ids", lambda *a, **k: None)
    monkeypatch.setattr(f, "_invalidate_caches", lambda *a, **k: None)


def test_gap_scan_historic_year_marks_permanent(config, monkeypatch):
    f = Facade(config)
    _stub_network(f, monkeypatch, present={"ewca/civ/2010/1", "ewca/civ/2010/2", "ewca/civ/2010/3"})
    res = f.gap_scan(court="ewca/civ", year=2010, stop_after_misses=4, max_probes=30)
    assert res["fetched"] == 3
    assert res["highest"] == 3
    assert res["historic"] is True
    assert res["absent"] >= 4  # stopped after the run of empties past no. 3
    st = f.gap_status(court="ewca/civ", year=2010)
    assert st["permanent_gaps"] >= 4
    assert st["pending_reprobe"] == 0


def test_gap_scan_current_year_marks_retry(config, monkeypatch):
    f = Facade(config)
    year = datetime.datetime.now(datetime.timezone.utc).year
    _stub_network(f, monkeypatch, present={f"uksc/{year}/1"})
    res = f.gap_scan(court="uksc", year=year, stop_after_misses=3, max_probes=20)
    assert res["fetched"] == 1
    assert res["historic"] is False
    st = f.gap_status(court="uksc", year=year)
    assert st["pending_reprobe"] >= 3       # current-year misses are re-probable
    assert st["permanent_gaps"] == 0


def test_gap_scan_skips_recorded_permanent_gaps_on_rerun(config, monkeypatch):
    f = Facade(config)
    _stub_network(f, monkeypatch, present={"ewhc/admin/2015/1"})
    f.gap_scan(court="ewhc/admin", year=2015, stop_after_misses=3, max_probes=15)
    # second run: the permanent gaps are skipped, so nothing new is recorded
    res2 = f.gap_scan(court="ewhc/admin", year=2015, stop_after_misses=3, max_probes=15)
    assert res2["absent"] == 0  # all misses already known → skipped
    cleared = f.clear_gap_markers(court="ewhc/admin", year=2015)
    assert "ewhc/admin/2015/" in cleared["cleared"]
