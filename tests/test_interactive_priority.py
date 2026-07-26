"""Interactive-priority throttle, the bounded embed queue, and the resume-attempt cap —
the guards added after a background embed job OOM-looped and doc opens hung behind a rebuild.
"""

import importlib

import raglex.interactive as I


def _reset():
    # each test controls the clock via note_interactive; start from "never active"
    importlib.reload(I)
    return I


def test_throttle_is_noop_when_idle(monkeypatch):
    mod = _reset()
    monkeypatch.setenv("RAGLEX_JOB_QUIET_WINDOW_S", "0.2")
    # no interactive request has ever been noted → nothing to yield to
    assert mod.throttle_for_interactive() == 0.0


def test_throttle_parks_while_recently_active_then_releases(monkeypatch):
    mod = _reset()
    monkeypatch.setenv("RAGLEX_JOB_QUIET_WINDOW_S", "0.3")
    monkeypatch.setenv("RAGLEX_JOB_MAX_YIELD_S", "5")
    mod.note_interactive()
    slept = mod.throttle_for_interactive()
    # parked roughly for the quiet window, then released on its own
    assert 0.2 <= slept <= 2.0
    # once quiet, the next call is a no-op again
    assert mod.throttle_for_interactive() == 0.0


def test_throttle_respects_the_yield_cap(monkeypatch):
    mod = _reset()
    monkeypatch.setenv("RAGLEX_JOB_QUIET_WINDOW_S", "100")  # would park ~forever
    monkeypatch.setenv("RAGLEX_JOB_MAX_YIELD_S", "0.3")     # …but the cap bounds it
    mod.note_interactive()
    slept = mod.throttle_for_interactive()
    assert slept <= 1.0  # a job is never starved past the cap


def test_throttle_can_be_disabled(monkeypatch):
    mod = _reset()
    monkeypatch.setenv("RAGLEX_INTERACTIVE_PRIORITY", "0")
    monkeypatch.setenv("RAGLEX_JOB_QUIET_WINDOW_S", "5")
    mod.note_interactive()
    assert mod.throttle_for_interactive() == 0.0


def test_throttle_stops_on_cancel(monkeypatch):
    mod = _reset()
    monkeypatch.setenv("RAGLEX_JOB_QUIET_WINDOW_S", "100")
    monkeypatch.setenv("RAGLEX_JOB_MAX_YIELD_S", "100")
    mod.note_interactive()
    # a cancelled job must not keep waiting for the user to go quiet
    slept = mod.throttle_for_interactive(cancel_check=lambda: True)
    assert slept <= 1.0


def test_resume_attempt_cap():
    from raglex import jobs
    assert jobs._resume_exhausted({"attempt": jobs.MAX_RESUME_ATTEMPTS, "kind": "embed",
                                   "job_id": "x"}) is True
    assert jobs._resume_exhausted({"attempt": 1, "kind": "embed", "job_id": "y"}) is False
