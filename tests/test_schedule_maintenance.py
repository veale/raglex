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
    assert schedule.is_enabled("auto-drain") is True           # others unaffected
    # persisted to the settings file (survives a fresh process reading env)
    import json
    assert "auto-embed" in json.loads((facade.config.settings_path).read_text())["RAGLEX_SCHEDULE"] \
        or "RAGLEX_SCHEDULE" in json.loads((facade.config.settings_path).read_text())


def test_task_cadence_override_and_reset(facade):
    facade.set_scheduled_task("authority", every_minutes=60)
    assert schedule.every_minutes("authority") == 60
    facade.set_scheduled_task("authority", remove=True)
    assert schedule.every_minutes("authority") == 1440         # back to default


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
