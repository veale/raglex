"""Sequential job queue: at the concurrency cap jobs queue and promote FIFO as slots free;
'add to queue' forces queuing; 'pause scheduled' holds scheduler-origin starts only; a
queued job can be cancelled before it runs."""

from __future__ import annotations

import threading
import time

from raglex import jobs as jobs_mod
from raglex.config import Config
from raglex.facade import Facade
from raglex.jobs import JobManager


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite",
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json", embed_provider="local-hashing",
        embed_model=None,
    )


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_overflow_queues_and_promotes_fifo(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_MAX_CONCURRENT_JOBS", "1")
    gate = threading.Event()
    started: list[int] = []

    def runner(f, p, cb, cancel):
        started.append(p["n"])
        gate.wait(5)
        return {"n": p["n"]}

    monkeypatch.setitem(jobs_mod.RUNNERS, "test-job", runner)
    jm = JobManager(Facade(_config(tmp_path)))

    r1 = jm.start("test-job", "a", {"n": 1})
    r2 = jm.start("test-job", "b", {"n": 2})
    r3 = jm.start("test-job", "c", {"n": 3})
    assert "queued" not in r1 and r2.get("queued") and r3.get("queued")
    assert _wait(lambda: started == [1])                 # only one runs at the cap
    with jm.facade._open() as (cat, _rs, _ts):
        assert len(cat.running_jobs()) == 1 and len(cat.queued_jobs()) == 2

    gate.set()                                            # release → cascade-promote 2 then 3
    assert _wait(lambda: sorted(started) == [1, 2, 3])
    with jm.facade._open() as (cat, _rs, _ts):
        assert len(cat.queued_jobs()) == 0


def test_add_to_queue_forces_queue_even_with_a_free_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_MAX_CONCURRENT_JOBS", "6")
    gate = threading.Event()
    monkeypatch.setitem(jobs_mod.RUNNERS, "test-job", lambda f, p, cb, cancel: gate.wait(5))
    jm = JobManager(Facade(_config(tmp_path)))

    r = jm.start("test-job", "a", {"n": 1}, queue=True)   # explicit "add to queue"
    assert r.get("queued")
    with jm.facade._open() as (cat, _rs, _ts):
        assert len(cat.queued_jobs()) == 1 and len(cat.running_jobs()) == 0
    gate.set()


def test_cancel_a_queued_job(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_MAX_CONCURRENT_JOBS", "1")
    gate = threading.Event()
    monkeypatch.setitem(jobs_mod.RUNNERS, "test-job", lambda f, p, cb, cancel: gate.wait(5))
    jm = JobManager(Facade(_config(tmp_path)))

    jm.start("test-job", "a", {"n": 1})                   # runs (blocked on gate)
    r2 = jm.start("test-job", "b", {"n": 2})              # queued
    res = jm.cancel(r2["job_id"])
    assert res.get("was_queued") and res.get("cancelled")
    with jm.facade._open() as (cat, _rs, _ts):
        assert len(cat.queued_jobs()) == 0
        assert cat.get_job(r2["job_id"])["status"] == "cancelled"
    gate.set()


def test_pause_holds_scheduler_origin_only(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_SCHEDULER_PAUSED", "1")
    monkeypatch.setitem(jobs_mod.RUNNERS, "test-job", lambda f, p, cb, cancel: {})
    facade = Facade(_config(tmp_path))

    sched = JobManager(facade, origin="scheduler")
    assert sched.start("test-job", "recurring", {}).get("paused") is True
    with facade._open() as (cat, _rs, _ts):
        assert len(cat.running_jobs()) == 0 and len(cat.queued_jobs()) == 0

    api = JobManager(facade, origin="api")               # manual work still runs
    assert "paused" not in api.start("test-job", "manual", {})


def test_max_concurrent_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGLEX_MAX_CONCURRENT_JOBS", "3")
    jm = JobManager(Facade(_config(tmp_path)))
    assert jm._max_concurrent() == 3
    monkeypatch.setenv("RAGLEX_MAX_CONCURRENT_JOBS", "not-a-number")
    assert jm._max_concurrent() == jobs_mod.MAX_CONCURRENT_JOBS   # falls back to the default
