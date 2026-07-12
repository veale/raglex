"""Background jobs (§8) — durable, cross-process, restartable.

Long operations (drain the worklist, re-scan the corpus, snowball from a seed) run in a
thread and report progress so the UI can show "fetching 5/30" instead of blocking on one
request. The registry backing them used to be a dict in the API process, which cost three
things worth having:

- **durability** — a deploy or crash erased a running job's history mid-run;
- **restartability** — a frozen job (its socket died when the host slept) could only be
  relaunched from the closure held in memory, which died with the process;
- **visibility** — the scheduler runs in a *different container*, so its auto-drain never
  appeared in the jobs panel at all. That is precisely why an auto-drain silently storing
  zero documents for seventeen days went unnoticed.

So a job is a **row**. Its work is named by ``kind`` and parameterised by ``params``, both
persisted, so any process can re-launch it from scratch — the work is idempotent (dedup
skips held documents, cool-down lists skip known-absent ones), so a restart only does
what's left. Cancellation is a flag on the row, which is why the UI can cancel a job
running inside the scheduler.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("raglex.jobs")

# Jobs that pass over the WHOLE corpus — pointless (and CPU-wasteful) to run two at once,
# so these stay one-at-a-time. Everything else (seed-from-text, harvest a category,
# radiate, expand-citing) is keyed to a specific input and may run simultaneously.
SINGLETON_KINDS = frozenset({
    "rescan-citations", "backfill-metadata", "backfill-edge-keys",
    "rebuild-citation-counts", "auto-drain", "harvest-hol", "match-reports",
    "rescan", "mine-parallel", "match-legislation", "match-echr", "harvest-echr",
    "suggest-matches",
})
MAX_CONCURRENT_JOBS = 6
# Keyed jobs deduped by (kind, params): don't start an identical one while it's in flight.
DEDUP_KINDS = frozenset({"run-watch", "gap-scan"})
# A "running" job whose heartbeat hasn't ticked in this long is almost certainly frozen —
# its worker thread is parked on a network socket that died when the host slept/woke. We
# can't kill the dead thread (Python can't), but we flag it so the UI offers a restart.
STALL_SECONDS = 150.0
# How often a worker asks the DB whether it's been cancelled. Cancellation crosses process
# boundaries via the row, so it can't be a local flag; but reading it on every progress
# tick would be a query per document.
CANCEL_POLL_SECONDS = 2.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        ts = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def fmt_progress(p: dict) -> str:
    """One human log line from a progress event: 'degree 1  5/40 — ukpga/2018/12 ✓'."""
    if not p:
        return ""
    parts = [str(p["stage"])] if p.get("stage") else []
    if p.get("total"):
        parts.append(f"{p.get('done', 0)}/{p['total']}")
    elif "done" in p:
        parts.append(str(p["done"]))
    if p.get("item"):
        parts.append("— " + str(p["item"]))
    if "ok" in p:
        parts.append("✓" if p["ok"] else "✗")
    if p.get("msg"):
        parts.append(str(p["msg"]))
    return "  ".join(parts).strip()


# kind → the facade call it names. Persisting (kind, params) instead of a closure is what
# makes a job survive the process that started it.
RUNNERS: dict[str, Callable] = {
    "rescan-citations": lambda f, p, cb, cancel: f.apply_rules(source=p.get("source"), on_progress=cb, cancel_check=cancel),
    "backfill-metadata": lambda f, p, cb, cancel: f.backfill_document_metadata(on_progress=cb),
    "backfill-edge-keys": lambda f, p, cb, cancel: f.backfill_edge_keys(on_progress=cb, cancel_check=cancel),
    "rebuild-citation-counts": lambda f, p, cb, cancel: f.rebuild_citation_counts(),
    "pull-ag-opinions": lambda f, p, cb, cancel: f.pull_ag_opinions(on_progress=cb, cancel_check=cancel),
    "harvest-all": lambda f, p, cb, cancel: f.harvest_all_references(**p, on_progress=cb, cancel_check=cancel),
    "auto-drain": lambda f, p, cb, cancel: f.harvest_all_references(**p, on_progress=cb, cancel_check=cancel),
    "radiate": lambda f, p, cb, cancel: f.radiate(**p, on_progress=cb, cancel_check=cancel),
    "expand-citing": lambda f, p, cb, cancel: f.expand_citing_cases(**p, on_progress=cb, cancel_check=cancel),
    "refresh-category": lambda f, p, cb, cancel: f.refresh_category(**p, on_progress=cb, cancel_check=cancel),
    "seed-text": lambda f, p, cb, cancel: f.seed_from_text(**p, on_progress=cb, cancel_check=cancel),
    "harvest-hol": lambda f, p, cb, cancel: f.harvest_house_of_lords(**p, on_progress=cb, cancel_check=cancel),
    "match-reports": lambda f, p, cb, cancel: f.match_report_citations(on_progress=cb, cancel_check=cancel),
    "import-bailii-corpus": lambda f, p, cb, cancel: f.import_bailii_corpus(**p, on_progress=cb, cancel_check=cancel),
    "mine-parallel": lambda f, p, cb, cancel: f.mine_parallel_citations(**p, on_progress=cb, cancel_check=cancel),
    "match-legislation": lambda f, p, cb, cancel: f.match_named_legislation(**p, on_progress=cb, cancel_check=cancel),
    "match-echr": lambda f, p, cb, cancel: f.match_echr_reports(**p, on_progress=cb, cancel_check=cancel),
    "rescan": lambda f, p, cb, cancel: f.rescan(**p, on_progress=cb, cancel_check=cancel),
    "suggest-matches": lambda f, p, cb, cancel: f.suggest_matches(**p, on_progress=cb, cancel_check=cancel),
    "harvest-echr": lambda f, p, cb, cancel: f.harvest_missing_echr(**p, on_progress=cb, cancel_check=cancel),
    "run-watch": lambda f, p, cb, cancel: f.run_watch(watch_id=p["watch_id"], on_progress=cb, cancel_check=cancel),
    "gap-scan": lambda f, p, cb, cancel: f.gap_scan(**p, on_progress=cb, cancel_check=cancel),
}


class JobManager:
    """Starts jobs, threads their progress into the ``jobs`` table, and reads them back.

    One instance per process. ``origin`` names the process ('api' / 'scheduler') so a
    restart can tell *its own* orphaned rows from another container's live ones.
    """

    def __init__(self, facade, *, origin: str = "api") -> None:
        self.facade = facade
        self.origin = origin
        # How long the job thread sleeps on each progress tick. A job runs in a thread
        # inside the API process; a CPU-bound loop (e.g. extracting 20k docs) would
        # otherwise hold the GIL and starve the web server until it's unreachable.
        # Sleeping RELEASES the GIL, so the event loop keeps serving requests.
        self.yield_s = float(os.environ.get("RAGLEX_JOB_YIELD_S") or 0.003)

    # -- lifecycle ---------------------------------------------------------
    def reap_orphans(self) -> int:
        """Mark this process's leftover 'running' rows as interrupted (called at startup).
        Their worker threads died with the previous process; without this they show as
        live forever."""
        with self.facade._open() as (cat, _rs, _ts):
            n = cat.orphan_running_jobs(self.origin)
            cat.prune_jobs()
        if n:
            log.info("marked %d orphaned %s job(s) as interrupted", n, self.origin)
        return n

    def start(self, kind: str, label: str, params: dict | None = None) -> dict:
        if kind not in RUNNERS:
            return {"error": f"unknown job kind {kind!r}"}
        params = params or {}
        with self.facade._open() as (cat, _rs, _ts):
            running = cat.running_jobs()
            if kind in SINGLETON_KINDS:
                for j in running:
                    if j["kind"] == kind:
                        return {"job_id": j["job_id"], "already_running": True}
            # Keyed jobs (a watch, a court/year gap-scan): don't launch an identical one while
            # one is already in flight — the scheduler ticks faster than a watch can finish, and
            # last_run_at only updates when it ends, so without this a slow watch double-runs.
            elif kind in DEDUP_KINDS:
                import json as _json

                want = _json.dumps(params, sort_keys=True)
                for j in running:
                    if j["kind"] == kind and _json.dumps(_json.loads(j["params_json"] or "{}"), sort_keys=True) == want:
                        return {"job_id": j["job_id"], "already_running": True}
            if len(running) >= MAX_CONCURRENT_JOBS:
                return {"error": f"too many jobs running ({len(running)}); let some finish first"}
            job_id = uuid.uuid4().hex[:8]
            cat.create_job(job_id, kind, label, params, origin=self.origin)
        threading.Thread(target=self._worker, args=(job_id, kind, params), daemon=True).start()
        return {"job_id": job_id}

    def _worker(self, job_id: str, kind: str, params: dict) -> None:
        state = {"progress": {}, "log": [], "cancel": False, "last_poll": 0.0, "last_write": 0.0}

        def cancel_check() -> bool:
            # Poll the row, not a local flag — the cancel may come from another process.
            if state["cancel"]:
                return True
            if time.monotonic() - state["last_poll"] >= CANCEL_POLL_SECONDS:
                state["last_poll"] = time.monotonic()
                try:
                    with self.facade._open() as (cat, _rs, _ts):
                        state["cancel"] = cat.job_cancelled(job_id)
                except Exception:  # noqa: BLE001 — a DB blip must not kill the job
                    pass
            return bool(state["cancel"])

        def on_progress(**p) -> None:
            state["progress"] = p
            line = fmt_progress(p)
            if line and (not state["log"] or state["log"][-1] != line):
                state["log"].append(line)
                if len(state["log"]) > 300:
                    del state["log"][:100]
            # The heartbeat is a write; throttle it to ~1/s or a 20k-document loop turns
            # into 20k UPDATEs.
            if time.monotonic() - state["last_write"] >= 1.0:
                state["last_write"] = time.monotonic()
                try:
                    with self.facade._open() as (cat, _rs, _ts):
                        cat.heartbeat_job(job_id, p, state["log"])
                except Exception:  # noqa: BLE001
                    pass
            if self.yield_s:
                time.sleep(self.yield_s)  # yield the GIL so the API never starves

        try:
            result = RUNNERS[kind](self.facade, params, on_progress, cancel_check)
            status = "cancelled" if state["cancel"] else "done"
            state["log"].append(f"— {status} —")
        except Exception as exc:  # noqa: BLE001 — surface to the poller, don't crash
            result, status = {"error": str(exc)}, "error"
            state["log"].append(f"✗ error: {exc}")
            log.exception("job %s (%s) failed", job_id, kind)
        try:
            with self.facade._open() as (cat, _rs, _ts):
                cat.finish_job(job_id, status, result, state["log"])
        except Exception:  # noqa: BLE001
            log.exception("could not record completion of job %s", job_id)

    # -- reads -------------------------------------------------------------
    @staticmethod
    def _row_to_dict(j, *, tail: int | None = None) -> dict:
        import json as _json

        def _load(raw, default):
            try:
                return _json.loads(raw) if raw else default
            except (ValueError, TypeError):
                return default

        running = j["status"] == "running"
        idle = _age_seconds(j["heartbeat_at"]) if running else 0.0
        logs = _load(j["log_json"], [])
        out = {
            "id": j["job_id"], "kind": j["kind"], "label": j["label"], "status": j["status"],
            "origin": j["origin"], "progress": _load(j["progress_json"], {}),
            "started_at": j["started_at"], "finished_at": j["finished_at"],
            "idle_s": round(idle, 1), "stalled": running and idle >= STALL_SECONDS,
            "last": (logs or [""])[-1],
            "result": _load(j["result_json"], None),
        }
        if tail is not None:
            out["log"] = logs[-tail:]
        return out

    def list(self, *, limit: int = 60) -> list[dict]:
        with self.facade._open() as (cat, _rs, _ts):
            return [self._row_to_dict(j) for j in cat.list_jobs(limit=limit)]

    def get(self, job_id: str, *, tail: int = 40) -> dict:
        with self.facade._open() as (cat, _rs, _ts):
            j = cat.get_job(job_id)
            return self._row_to_dict(j, tail=tail) if j else {"status": "unknown"}

    def cancel(self, job_id: str) -> dict:
        with self.facade._open() as (cat, _rs, _ts):
            return {"job_id": job_id, "cancelling": cat.request_job_cancel(job_id)}

    def restart(self, job_id: str) -> dict:
        """Re-launch a job from where its persisted data left off — for a frozen job (the
        host slept and its network socket died) or any finished/cancelled one. Rebuilt from
        the stored (kind, params), so it works even across the restart that lost the
        original process."""
        import json as _json

        with self.facade._open() as (cat, _rs, _ts):
            j = cat.get_job(job_id)
            if not j:
                return {"error": "unknown job"}
            kind, label = j["kind"], j["label"]
            try:
                params = _json.loads(j["params_json"] or "{}")
            except (ValueError, TypeError):
                params = {}
            if j["status"] == "running":
                cat.request_job_cancel(job_id)  # ask the old thread to stop at a checkpoint
                cat.finish_job(job_id, "cancelled", {"superseded_by_restart": True})
        res = self.start(kind, label, params)
        res["restarted_from"] = job_id
        return res
