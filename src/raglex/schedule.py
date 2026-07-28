"""Per-task scheduler toggles (§8) — an add/remove, on/off system for recurring work.

The scheduler used to be all-or-nothing: one ``RAGLEX_SCHEDULER_PAUSED`` flag stopped
*everything* (watches, harvests, embed, roll-ups). That's too blunt — you often want, say,
auto-embed OFF (after dropping embeddings, so it doesn't refill) while auto-drain and the
daily roll-ups keep running. So each recurring task is now individually **enable/disable +
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


# The recurring tasks the scheduler runs. ``default_minutes=None`` = runs every scheduler
# tick when enabled (the fast, self-throttling drains); a number = a minimum interval.
TASKS: tuple[TaskSpec, ...] = (
    TaskSpec("watches", True, None, "run due saved keyword watches"),
    TaskSpec("nightly-harvest", True, None, "overnight idle full drain of the routable worklist"),
    TaskSpec("auto-drain", True, None, "drain the routable hanging-reference worklist each tick"),
    TaskSpec("auto-embed", True, None, "index newly-texted documents into the embedding family each tick"),
    TaskSpec("canlii-enrich", True, None, "drain the rate-limited CanLII enrichment queue"),
    TaskSpec("au-cth-repair", True, None, "trickle-repair Australian Cth citations"),
    TaskSpec("effects", True, 720, "re-check legislation.gov.uk unapplied-effects queue"),
    TaskSpec("counts", True, 10080, "citation-count roll-up (the snowball aggregate)"),
    TaskSpec("authority", True, 1440, "PageRank authority rebuild"),
    TaskSpec("analyze", True, 1440, "refresh Postgres planner statistics (ANALYZE)"),
    TaskSpec("gazetteer", True, 10080, "top up the statute gazetteer from legislation.gov.uk"),
    TaskSpec("maintenance", False, 1440, "serial DB maintenance + safe repair pass (off by default)"),
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
            "default_enabled": t.default_enabled,
            "overridden": bool(o),
        })
    return out


def set_task(settings, name: str, *, enabled: bool | None = None,
             every_minutes: int | None = None, remove: bool = False) -> dict:
    """Add/update/remove a task override in the persisted ``RAGLEX_SCHEDULE`` map. ``remove``
    reverts the task to its default. Returns the new task list."""
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
        current[name] = entry
    settings.update({"RAGLEX_SCHEDULE": json.dumps(current)})
    settings.apply_to_env()  # take effect in this process immediately
    return {"tasks": list_tasks()}
