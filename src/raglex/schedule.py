"""Per-task scheduler toggles (§8) — an add/remove, on/off system for recurring work.

The scheduler used to be all-or-nothing: one ``RAGLEX_SCHEDULER_PAUSED`` flag stopped
*everything* (watches, harvests, embed, roll-ups). That's too blunt — you often want, say,
auto-embed OFF (after dropping embeddings, so it doesn't refill) while the nightly harvest
and the daily roll-ups keep running. So each recurring task is now individually **enable/disable +
cadence**-controllable, persisted in one settings row (``RAGLEX_SCHEDULE``, a JSON map of
overrides), and the scheduler consults it before each task.

Back-compat: the global pause still wins (pausing holds every task); with no overrides every
task keeps its historical default, so nothing changes until you touch a toggle.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    default_enabled: bool
    default_minutes: int | None   # cadence for time-based tasks; None = "every tick"
    description: str
    # Hour (UTC, 0-23) this task is allowed to start in, or None for "any hour". A
    # corpus-wide roll-up costs the same whenever it runs, so it may as well run when
    # nobody is reading — cadence alone would fire it at whatever time of day the last
    # run happened to land on.
    default_hour: int | None = None


# The recurring tasks the scheduler runs. ``default_minutes=None`` = runs every scheduler
# tick when enabled (the fast, self-throttling drains); a number = a minimum interval.
TASKS: tuple[TaskSpec, ...] = (
    TaskSpec("watches", True, None, "run due saved keyword watches"),
    TaskSpec("nightly-harvest", True, None, "02:00 full drain of the routable worklist"),
    TaskSpec("auto-embed", True, None, "index newly-texted documents into the embedding family each tick"),
    TaskSpec("canlii-enrich", True, None, "drain the rate-limited CanLII enrichment queue"),
    # 60, not 720: the loop has always run this hourly with the interval hardcoded, so the
    # declared 12h cadence was never what happened. Now that the cadence is read from here,
    # it has to say what the scheduler actually does.
    TaskSpec("effects", True, 60, "re-check legislation.gov.uk unapplied-effects queue"),
    # Both are corpus-wide walks over a 17M-edge graph, and both feed RANKING, not
    # correctness: a document harvested since the last run is still found, still read,
    # still cited — it just carries a stale authority score until the next pass. So they
    # run weekly, in the small hours, rather than chasing every harvest.
    TaskSpec("counts", True, 10080, "citation-count roll-up (the snowball aggregate)",
             default_hour=4),
    TaskSpec("authority", True, 10080, "PageRank authority rebuild", default_hour=4),
    TaskSpec("analyze", True, 1440, "refresh Postgres planner statistics (ANALYZE)"),
    # OFF: the scheduled weekly passes above are enough. On, every corpus-growing job
    # also triggers the count + PageRank roll-ups (debounced) — fresher ranking, at the
    # cost of repeated whole-graph walks while harvesting.
    TaskSpec("postprocess-rollups", False, None,
             "also rebuild counts + PageRank after each harvest (off by default; "
             "the weekly 'counts'/'authority' passes normally suffice)"),
    TaskSpec("gazetteer", True, 10080, "top up the statute gazetteer from legislation.gov.uk"),
    TaskSpec("maintenance", False, 1440, "serial DB maintenance + safe repair pass (off by default)"),
    TaskSpec("static-bundle", False, 10080,
             "rebuild the configured static editions into the export folder (off by default)"),
    TaskSpec("eu-consolidations", True, 10080,
             "walk Cellar sector-0 and import every dated EU consolidation"),
    TaskSpec("eu-pending-cases", True, 1440,
             "import pending C/T case notices and retire them when English decisions arrive"),
    TaskSpec("eu-legislation-enrich", False, 1440, "harvest EU act-to-act relationships (repeals/amends/legal-basis) from CELLAR"),
    TaskSpec("eu-case-names", False, 1440, "pull CJEU case names + subject tags from the EUR-Lex webservice (needs credentials)"),
)
_BY_NAME = {t.name: t for t in TASKS}


def _overrides() -> dict:
    raw = os.environ.get("RAGLEX_SCHEDULE")
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def is_enabled(name: str) -> bool:
    spec = _BY_NAME.get(name)
    default = spec.default_enabled if spec else True
    ov = _overrides().get(name)
    if isinstance(ov, dict) and "enabled" in ov:
        return bool(ov["enabled"])
    return default


def every_minutes(name: str, default: int | None = None) -> int | None:
    spec = _BY_NAME.get(name)
    base = default if default is not None else (spec.default_minutes if spec else None)
    ov = _overrides().get(name)
    if isinstance(ov, dict) and ov.get("every_minutes"):
        try:
            return max(1, int(ov["every_minutes"]))
        except (TypeError, ValueError):
            return base
    return base


def at_hour(name: str) -> int | None:
    """The UTC hour this task may start in, or ``None`` for any hour."""
    spec = _BY_NAME.get(name)
    base = spec.default_hour if spec else None
    ov = _overrides().get(name)
    if isinstance(ov, dict) and "at_hour" in ov:
        if ov["at_hour"] is None:
            return None
        try:
            return max(0, min(23, int(ov["at_hour"])))
        except (TypeError, ValueError):
            return base
    return base


def in_window(name: str, now=None) -> bool:
    """Is this task allowed to start RIGHT NOW?

    Cadence says how often; this says when. A task with an hour set waits for it, so a
    weekly roll-up lands at 04:00 rather than drifting to whenever the previous run
    happened to finish. Tasks without an hour are always in window.
    """
    hour = at_hour(name)
    if hour is None:
        return True
    from datetime import datetime, timezone

    return (now or datetime.now(timezone.utc)).hour == hour


def list_tasks() -> list[dict]:
    """Every known task with its effective enabled/cadence + whether it's overridden — the
    payload the on/off UI renders."""
    ov = _overrides()
    out = []
    for t in TASKS:
        o = ov.get(t.name) if isinstance(ov.get(t.name), dict) else {}
        out.append({
            "name": t.name,
            "description": t.description,
            "enabled": bool(o["enabled"]) if "enabled" in o else t.default_enabled,
            "every_minutes": every_minutes(t.name),
            "at_hour": at_hour(t.name),
            "default_enabled": t.default_enabled,
            "overridden": bool(o),
        })
    return out


def set_task(settings, name: str, *, enabled: bool | None = None,
             every_minutes: int | None = None, remove: bool = False,
             at_hour: int | str | None = None) -> dict:
    """Add/update/remove a task override in the persisted ``RAGLEX_SCHEDULE`` map. ``remove``
    reverts the task to its default. ``at_hour`` pins the task to one UTC hour; pass the
    string ``"any"`` to clear it. Returns the new task list."""
    if name not in _BY_NAME:
        return {"error": f"unknown task {name!r}", "known": sorted(_BY_NAME)}
    current = _overrides()
    if remove:
        current.pop(name, None)
    else:
        entry = current.get(name) if isinstance(current.get(name), dict) else {}
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        if every_minutes is not None:
            entry["every_minutes"] = max(1, int(every_minutes))
        if at_hour is not None:
            # "any" is how a caller says "no hour", which None cannot express here —
            # None already means "leave this field alone".
            entry["at_hour"] = (
                None if str(at_hour).strip().lower() in ("any", "none", "")
                else max(0, min(23, int(at_hour))))
        current[name] = entry
    settings.update({"RAGLEX_SCHEDULE": json.dumps(current)})
    settings.apply_to_env()  # take effect in this process immediately
    return {"tasks": list_tasks()}
