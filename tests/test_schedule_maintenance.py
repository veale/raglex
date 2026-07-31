"""Per-task scheduler toggles + the serial maintenance job (§8)."""

from __future__ import annotations

import pytest

from raglex.config import Config
from raglex.facade import Facade
from raglex import schedule
from raglex.maintenance import build_plan, run_maintenance


@pytest.fixture
def facade(tmp_path, monkeypatch):
    monkeypatch.delenv("RAGLEX_SCHEDULE", raising=False)
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "c.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "s.json",
        embed_provider="local-hashing", embed_model=None))


def test_task_toggle_persists_and_is_scoped(facade):
    assert schedule.is_enabled("auto-embed") is True          # default on
    facade.set_scheduled_task("auto-embed", enabled=False)
    assert schedule.is_enabled("auto-embed") is False          # turned off
    assert schedule.is_enabled("nightly-harvest") is True      # others unaffected
    # persisted to the settings file (survives a fresh process reading env)
    import json
    assert "auto-embed" in json.loads((facade.config.settings_path).read_text())["RAGLEX_SCHEDULE"] \
        or "RAGLEX_SCHEDULE" in json.loads((facade.config.settings_path).read_text())


def test_task_cadence_override_and_reset(facade):
    facade.set_scheduled_task("authority", every_minutes=60)
    assert schedule.every_minutes("authority") == 60
    facade.set_scheduled_task("authority", remove=True)
    # Weekly by default: a whole-graph PageRank walk feeds ranking, not correctness.
    assert schedule.every_minutes("authority") == 10080        # back to default


def test_heavy_rollups_default_to_the_quiet_hours(facade):
    """Cadence says how often; at_hour says when. Without it a weekly roll-up drifts to
    whatever time of day the previous run happened to land on."""
    from datetime import datetime, timezone

    assert schedule.at_hour("authority") == 4
    assert schedule.at_hour("counts") == 4
    assert schedule.at_hour("watches") is None                 # unpinned tasks: any hour

    at_four = datetime(2026, 8, 1, 4, 30, tzinfo=timezone.utc)
    at_noon = datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)
    assert schedule.in_window("authority", at_four) is True
    assert schedule.in_window("authority", at_noon) is False
    assert schedule.in_window("watches", at_noon) is True       # never gated

    facade.set_scheduled_task("authority", at_hour=23)
    assert schedule.at_hour("authority") == 23
    facade.set_scheduled_task("authority", at_hour="any")
    assert schedule.at_hour("authority") is None
    assert schedule.in_window("authority", at_noon) is True


def test_post_harvest_rollups_are_off_by_default(facade):
    """The weekly passes cover it; chaining a whole-graph walk off every harvest was
    ~48 PageRank rebuilds a day to keep a ranking aggregate slightly fresher."""
    assert schedule.is_enabled("postprocess-rollups") is False


def test_maintenance_default_off(facade):
    assert schedule.is_enabled("maintenance") is False


def test_maintenance_plan_and_run_are_serial_and_safe(facade):
    plan = build_plan(facade, {"no_rescans": True})
    assert plan[0].startswith("repair:") and "analyze" in plan and "authority" in plan
    # runs every step once, in order, without error on an empty corpus (all idempotent no-ops)
    result = run_maintenance(facade, {"no_rescans": True},
                             on_progress=lambda **p: None, cancel_check=lambda: False)
    assert result["total"] == len(plan)
    assert [r["step"] for r in result["results"]] == plan
    assert not any(isinstance(r["result"], dict) and r["result"].get("error")
                   for r in result["results"])


def test_maintenance_cancellation_is_honoured(facade):
    calls = {"n": 0}
    def cancel():
        calls["n"] += 1
        return calls["n"] > 1          # cancel after the first step
    result = run_maintenance(facade, {"no_rescans": True},
                             on_progress=lambda **p: None, cancel_check=cancel)
    assert result.get("cancelled") is True
    assert result["completed"] < result["total"]


def test_system_key_persistence_fix(facade):
    """The bug this uncovered: update() dropped keys outside KNOWN_SETTINGS, so the auth
    session secret / passkeys / OAuth clients never persisted."""
    facade.update_settings({"RAGLEX_SESSION_SECRET": "abc123"})
    assert facade.settings.resolve("RAGLEX_SESSION_SECRET") == "abc123"


def test_nightly_harvest_marker_survives_a_restart(tmp_path, monkeypatch):
    """"Has it run today" must be read off the jobs table, not a variable in the
    scheduler process: with an in-process marker, every deploy after 02:00 kicked off a
    fresh full drain of the whole routable worklist."""
    import os
    import time

    monkeypatch.setenv("RAGLEX_DATA_DIR", str(tmp_path))
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config.from_env())
    today = time.strftime("%Y-%m-%d", time.localtime())
    with f._open() as (cat, _rs, _ts):
        assert not [r for r in cat.recent_jobs("harvest-all", limit=5)]
        cat.create_job("j1", "harvest-all", "overnight harvest", {})
        rows = cat.recent_jobs("harvest-all", limit=5)
    assert [r["started_at"][:10] for r in rows] == [today]
