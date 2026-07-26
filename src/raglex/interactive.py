"""Interactive-request priority — let UI/MCP reads take precedence over background jobs.

Background jobs run as worker threads inside the same process as the API, and everything
hits one Postgres. Postgres has no per-query priority, so on a small (RAM-starved) box a
heavy job's sequential disk IO evicts the shared-buffer pages an interactive document view
needs — turning a sub-second citator open into tens of seconds (observed: "Data Protection
Act 2018" taking 20-40s while a graph rebuild ran).

This module is the cooperative lever. Every genuine interactive read stamps a monotonic
timestamp (``note_interactive``); job loops call ``throttle_for_interactive`` at their
natural yield points (the shared job-progress callback), which briefly parks the worker
while a user is active. It is:

* **bounded** — a job can never be starved longer than ``RAGLEX_JOB_MAX_YIELD_S`` per
  yield point, so backlog work still drains during sustained interactive use;
* **cheap** — when no request has landed inside the quiet window it is a single monotonic
  read and returns immediately;
* **opt-out** — ``RAGLEX_INTERACTIVE_PRIORITY=0`` restores the old fixed GIL-yield.

High-frequency pollers (the Jobs panel's ``/jobs`` + ``/health``) must NOT stamp activity,
or the ~1 Hz poll would hold the quiet window open forever and jobs would never progress;
the middleware that calls ``note_interactive`` excludes them (see ``web/app.py``).
"""

from __future__ import annotations

import os
import threading
import time

_last_interactive: float = 0.0
_lock = threading.Lock()


def note_interactive() -> None:
    """Record that an interactive (user-facing) request is being served, now."""
    global _last_interactive
    with _lock:
        _last_interactive = time.monotonic()


def seconds_since_interactive() -> float:
    """Seconds since the last interactive request; ``inf`` if there has never been one."""
    with _lock:
        last = _last_interactive
    return float("inf") if last == 0.0 else time.monotonic() - last


def _flag(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip().lower()


def enabled() -> bool:
    return _flag("RAGLEX_INTERACTIVE_PRIORITY", "1") not in ("0", "off", "false", "no")


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def quiet_window_s() -> float:
    """How long after an interactive request jobs keep yielding to it."""
    return _float_env("RAGLEX_JOB_QUIET_WINDOW_S", 1.5)


def max_yield_s() -> float:
    """Cap on how long a single job yield point will park, so jobs can't be starved."""
    return _float_env("RAGLEX_JOB_MAX_YIELD_S", 20.0)


def throttle_for_interactive(cancel_check=None) -> float:
    """Park the calling (job) thread while interactive activity is recent, bounded by
    ``max_yield_s``. Returns the seconds actually slept — 0.0 when priority is off or no
    request landed inside the quiet window, so the caller can fall back to its normal
    tiny GIL-yield. Honours ``cancel_check`` so a cancelled job stops waiting at once."""
    if not enabled():
        return 0.0
    window = quiet_window_s()
    if window <= 0.0:
        return 0.0
    cap = max_yield_s()
    slept = 0.0
    step = min(0.05, window)
    while seconds_since_interactive() < window:
        if slept >= cap or (cancel_check and cancel_check()):
            break
        time.sleep(step)
        slept += step
    return slept
