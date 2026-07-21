"""Durable, cross-process background jobs (§8) and the alerts that watch them.

The registry is a table, not a process-memory dict, for three reasons this file pins:
history survives a restart, a job started by one process is visible to another (the
scheduler's auto-drain in the API's panel), and cancellation crosses that boundary. The
drain-stall alert is the safeguard that would have caught the poisoned-skip-list bug in a
day instead of seventeen days.
"""

from __future__ import annotations

import tempfile

from raglex.config import Config
from raglex.facade import Facade
from raglex.jobs import JobManager
from raglex.ops import AlertThresholds, check_alerts


def _facade() -> Facade:
    import os

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


def _finished_job(f: Facade, kind: str, result: dict) -> str:
    """Seed a completed job row directly — the shape the manager's worker leaves behind."""
    import uuid

    jid = uuid.uuid4().hex[:8]
    with f._open() as (cat, _rs, _ts):
        cat.create_job(jid, kind, kind, {})
        cat.finish_job(jid, "done", result)
    return jid


def test_job_row_is_visible_across_managers():
    # Two managers = two "processes" over one DB. What one records, the other sees — this
    # is exactly how the scheduler's auto-drain shows up in the API's jobs panel.
    f = _facade()
    _finished_job(f, "auto-drain", {"harvested": 3})
    api_view = JobManager(f, origin="api").list()
    assert len(api_view) == 1 and api_view[0]["kind"] == "auto-drain"
    assert api_view[0]["result"] == {"harvested": 3}


def test_orphan_reaping_only_touches_own_origin():
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        cat.create_job("api1", "harvest-all", "x", {}, origin="api")
        cat.create_job("sch1", "auto-drain", "x", {}, origin="scheduler")
    # the API restarts: its own leftover 'running' row is interrupted, the scheduler's isn't
    assert JobManager(f, origin="api").reap_orphans() == 1
    with f._open() as (cat, _rs, _ts):
        assert cat.get_job("api1")["status"] == "interrupted"
        assert cat.get_job("sch1")["status"] == "running"


def test_cancel_crosses_the_process_boundary():
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        cat.create_job("j1", "harvest-all", "x", {}, origin="scheduler")
    # the API cancels a job the scheduler is running; the worker polls the flag off the row
    JobManager(f, origin="api").cancel("j1")
    with f._open() as (cat, _rs, _ts):
        assert cat.job_cancelled("j1") is True


def test_restart_rebuilds_from_stored_params(monkeypatch):
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        cat.create_job("old", "harvest-all", "harvest all", {"limit": 7}, origin="api")
        cat.finish_job("old", "error", {"error": "socket died"})
    started: dict = {}
    mgr = JobManager(f, origin="api")

    def fake_start(kind, label, params, **resume):
        started.update(kind=kind, params=params, resume=resume)
        return {"job_id": "new"}
    monkeypatch.setattr(mgr, "start", fake_start)

    res = mgr.restart("old")
    assert res["restarted_from"] == "old"
    assert started["kind"] == "harvest-all" and started["params"] == {"limit": 7}
    assert started["resume"]["resumed_from"] == "old"
    assert started["resume"]["root_job_id"] == "old"


def test_checkpoint_is_distinct_from_display_progress():
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        cat.create_job("scan", "rescan-citations", "scan", {}, root_job_id="scan",
                       resume_policy="checkpoint")
        cat.heartbeat_job("scan", {"stage": "scan", "done": 2}, [],
                          checkpoint={"completed": 1, "last_id": "doc/1"})
        row = JobManager(f).get("scan")
    assert row["progress"]["done"] == 2
    assert row["resume"]["checkpoint"] == {"completed": 1, "last_id": "doc/1"}


def test_scan_run_marker_excludes_only_committed_documents(catalogue):
    from raglex.core.models import DocType, Record

    for sid in ("doc/a", "doc/b", "doc/c"):
        rec = Record(source="x", stable_id=sid, doc_type=DocType.JUDGMENT,
                     raw_bytes=sid.encode(), text="text")
        rec.ensure_payload_hash()
        catalogue.upsert_document(rec, text_path="dummy")
    catalogue.mark_extracted("doc/a", run_id="root-1")
    catalogue.mark_extracted("doc/b", run_id="another-run")
    assert set(catalogue.text_document_ids(exclude_extraction_run_id="root-1")) == {"doc/b", "doc/c"}


def test_running_restart_cancels_before_replacement(monkeypatch):
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        cat.create_job("live", "rescan-citations", "scan", {}, origin="api")
    mgr = JobManager(f, origin="api")
    monkeypatch.setattr(mgr, "start", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("replacement must not overlap live worker")))
    result = mgr.restart("live")
    assert result["cancelling"] and result["restart_when_stopped"]
    with f._open() as (cat, _rs, _ts):
        assert cat.job_cancelled("live")


def test_startup_auto_resumes_only_declared_safe_kinds(monkeypatch):
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        cat.create_job("scan", "rescan-citations", "scan", {}, origin="api")
        cat.create_job("rank", "rebuild-authority", "rank", {}, origin="api")
    resumed = []
    mgr = JobManager(f, origin="api")
    monkeypatch.setattr(mgr, "_resume_row", lambda row: resumed.append(row["job_id"]))
    assert mgr.reap_orphans(auto_resume=True) == 2
    assert resumed == ["scan"]


def test_job_status_exposes_resume_lineage_and_liveness():
    f = _facade()
    with f._open() as (cat, _rs, _ts):
        cat.create_job("try2", "rescan-citations", "scan", {"_resume_run_id": "root"},
                       root_job_id="root", resumed_from="try1",
                       resume_policy="checkpoint", attempt=2)
    row = JobManager(f).get("try2")
    assert row["resume"] == {
        "policy": "checkpoint", "root_job_id": "root", "resumed_from": "try1",
        "attempt": 2, "checkpoint": {},
    }
    assert row["process_alive"] is True


# -- the alert that would have caught the seventeen-day silence --------------

def test_drain_stall_alert_fires_when_every_reference_is_cooling_off():
    f = _facade()
    t = AlertThresholds(drain_window=3)
    for _ in range(3):
        _finished_job(f, "auto-drain", {"attempted": 0, "harvested": 0, "skipped_recent_fail": 5000})
    with f._open() as (cat, _rs, _ts):
        alerts = check_alerts(cat, t)
    codes = {a.code for a in alerts}
    assert "drain_all_cooling_off" in codes


def test_no_drain_alert_while_it_is_still_harvesting():
    f = _facade()
    t = AlertThresholds(drain_window=3)
    _finished_job(f, "auto-drain", {"attempted": 10, "harvested": 4})
    for _ in range(2):
        _finished_job(f, "auto-drain", {"attempted": 10, "harvested": 0})
    with f._open() as (cat, _rs, _ts):
        alerts = check_alerts(cat, t)
    assert not any(a.code.startswith("drain_") for a in alerts)


def test_drain_alert_waits_for_enough_history():
    f = _facade()
    t = AlertThresholds(drain_window=5)
    for _ in range(2):  # fewer runs than the window → not enough to judge
        _finished_job(f, "auto-drain", {"attempted": 0, "skipped_recent_fail": 100})
    with f._open() as (cat, _rs, _ts):
        assert not any(a.code.startswith("drain_") for a in check_alerts(cat, t))
