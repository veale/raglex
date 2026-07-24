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

So a job is a **row**. Its work is named by ``kind`` and parameterised by ``params``.
Restart semantics are explicit per kind: citation scans use committed document markers;
imports rediscover but deduplicate durable records; short graph rebuilds restart as a
whole. Attempts retain a root lineage and checkpoint. Cancellation is a flag on the row,
which is why the UI can cancel a job running inside the scheduler.
"""

from __future__ import annotations

import json
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
    "rescan-citations", "backfill-metadata", "backfill-edge-keys", "repair-au-cth",
    "backfill-eu-stubs",
    "rebuild-citation-counts", "rebuild-authority", "auto-drain", "match-reports",
    "rescan", "mine-parallel", "match-legislation", "match-echr", "harvest-echr",
    "suggest-matches", "classify-guidance",
    # one relation-range cursor over the whole graph — two would double-resolve ranges
    "finish-bulk-postprocess",
    # one metered walk of the Canadian enrichment queue — two would double-spend
    # the CanLII budget on the same head-of-queue documents
    "canlii-enrich",
    # only ever one indexing pass: two would race over the same pending_embedding queue
    "embed",
})
MAX_CONCURRENT_JOBS = 6
# Keyed jobs deduped by (kind, params): don't start an IDENTICAL one while it's in flight,
# but different-parameter runs proceed. harvest-all is here (not a blanket singleton): each
# click targets ONE adapter (a corpus-map category) and drains a disjoint candidate set, so
# a us-caselaw harvest must not be blocked by a running uk-legislation one. Two clicks of the
# SAME category still dedup, and the nightly whole-queue drain (no adapter → distinct params)
# dedups against a second whole-queue drain but no longer blocks the per-category buttons.
DEDUP_KINDS = frozenset({"run-watch", "gap-scan", "harvest-source", "harvest-all"})

# Resume is an explicit contract, not a blanket promise that "idempotent" means no
# repeated work. ``checkpoint`` jobs stamp each completed document with a stable root
# run id. ``deduplicate`` jobs restart discovery but cheaply skip durable outputs.
# ``restart`` jobs are safe to run again but have no useful mid-phase cursor.
RESUME_POLICIES = {
    "rescan-citations": "checkpoint", "rescan": "checkpoint",
    "harvest-source": "deduplicate", "harvest-all": "deduplicate",
    "auto-drain": "deduplicate", "embed": "deduplicate",
    "import-bailii-corpus": "deduplicate", "import-bailii-zip": "deduplicate",
    "import-bailii-dir": "deduplicate", "import-bailii-parquet": "deduplicate",
    "import-indian-sci": "deduplicate", "import-sg-seed": "deduplicate",
    "import-westlaw-zip": "deduplicate", "import-westlaw-dir": "deduplicate",
    "import-caselaw-zip": "deduplicate", "import-caselaw-dir": "deduplicate",
    "gap-scan": "deduplicate", "repair-au-cth": "deduplicate",
    # resumes from the persisted relation-id / tag cursors (see _resume_row)
    "finish-bulk-postprocess": "checkpoint",
    # resumes the whole-source reparse from the last stable_id checkpoint
    "reparse-source": "checkpoint",
    # resumes the whole-source citation re-anchor from the last stable_id checkpoint
    "reanchor-citations": "checkpoint",
}
AUTO_RESUME_KINDS = frozenset(RESUME_POLICIES)
# All three write the citations table; a re-anchor and a rescan of the SAME source must
# not run at once (they'd race the same offsets), but disjoint sources may.
_SCAN_KINDS = frozenset({"rescan-citations", "rescan", "reanchor-citations"})


def scheduler_paused() -> bool:
    """Whether the operator has paused the scheduler's recurring jobs + due watches
    (RAGLEX_SCHEDULER_PAUSED, UI-toggleable). Manual and queued jobs are unaffected."""
    return str(os.environ.get("RAGLEX_SCHEDULER_PAUSED") or "").strip().lower() in (
        "1", "true", "on", "yes")


def _scan_scope(kind: str, params: dict) -> str | None:
    return (params.get("source") or "*") if kind in _SCAN_KINDS else None


def _scan_conflict(kind: str, params: dict, running_row) -> bool:
    """Two extraction passes may coexist only when their source sets are disjoint."""
    if kind not in _SCAN_KINDS or running_row["kind"] not in _SCAN_KINDS:
        return False
    import json as _json
    try:
        other = _json.loads(running_row["params_json"] or "{}")
    except (ValueError, TypeError):
        other = {}
    a, b = _scan_scope(kind, params), _scan_scope(running_row["kind"], other)
    return a == "*" or b == "*" or a == b
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
    "rescan-citations": lambda f, p, cb, cancel: f.apply_rules(
        source=p.get("source"), run_id=p.get("_resume_run_id"),
        on_progress=cb, cancel_check=cancel),
    "backfill-metadata": lambda f, p, cb, cancel: f.backfill_document_metadata(on_progress=cb),
    "backfill-edge-keys": lambda f, p, cb, cancel: f.backfill_edge_keys(on_progress=cb, cancel_check=cancel),
    # re-fetch EU instruments stored as bare metadata stubs (a transient harvest
    # failure left ~7,400 heavily-cited acts as dead ends)
    "backfill-eu-stubs": lambda f, p, cb, cancel: f.backfill_eu_stubs(
        limit=int(p.get("limit") or 500), on_progress=cb, cancel_check=cancel),
    "rebuild-citation-counts": lambda f, p, cb, cancel: f.rebuild_citation_counts(),
    "rebuild-authority": lambda f, p, cb, cancel: f.rebuild_authority(on_progress=cb, cancel_check=cancel),
    "pull-ag-opinions": lambda f, p, cb, cancel: f.pull_ag_opinions(on_progress=cb, cancel_check=cancel),
    "harvest-all": lambda f, p, cb, cancel: f.harvest_all_references(**p, on_progress=cb, cancel_check=cancel),
    "auto-drain": lambda f, p, cb, cancel: f.harvest_all_references(**p, on_progress=cb, cancel_check=cancel),
    "radiate": lambda f, p, cb, cancel: f.radiate(**p, on_progress=cb, cancel_check=cancel),
    "expand-citing": lambda f, p, cb, cancel: f.expand_citing_cases(**p, on_progress=cb, cancel_check=cancel),
    "refresh-category": lambda f, p, cb, cancel: f.refresh_category(**p, on_progress=cb, cancel_check=cancel),
    "seed-text": lambda f, p, cb, cancel: f.seed_from_text(**p, on_progress=cb, cancel_check=cancel),
    "match-reports": lambda f, p, cb, cancel: f.match_report_citations(on_progress=cb, cancel_check=cancel),
    "import-bailii-corpus": lambda f, p, cb, cancel: f.import_bailii_corpus(**p, on_progress=cb, cancel_check=cancel),
    "import-bailii-zip": lambda f, p, cb, cancel: f.import_bailii_zip(**p, on_progress=cb, cancel_check=cancel),
    "import-bailii-dir": lambda f, p, cb, cancel: f.import_bailii_dir(**p, on_progress=cb, cancel_check=cancel),
    "import-bailii-parquet": lambda f, p, cb, cancel: f.import_bailii_parquet(**p, on_progress=cb, cancel_check=cancel),
    "import-indian-sci": lambda f, p, cb, cancel: f.import_indian_sci(**p, on_progress=cb, cancel_check=cancel),
    "import-sg-seed": lambda f, p, cb, cancel: f.import_sg_seed(**p, on_progress=cb, cancel_check=cancel),
    "repair-au-cth": lambda f, p, cb, cancel: f.repair_au_cth(**p, on_progress=cb, cancel_check=cancel),
    "import-westlaw-zip": lambda f, p, cb, cancel: f.import_westlaw_zip(**p, on_progress=cb, cancel_check=cancel),
    "import-westlaw-dir": lambda f, p, cb, cancel: f.import_westlaw_dir(**p, on_progress=cb, cancel_check=cancel),
    "import-caselaw-zip": lambda f, p, cb, cancel: f.import_caselaw_zip(**p, on_progress=cb, cancel_check=cancel),
    "import-caselaw-dir": lambda f, p, cb, cancel: f.import_caselaw_dir(**p, on_progress=cb, cancel_check=cancel),
    "embed": lambda f, p, cb, cancel: f.embed(**p, on_progress=cb, cancel_check=cancel),
    "classify-guidance": lambda f, p, cb, cancel: f.reclassify_guidance(**p, on_progress=cb, cancel_check=cancel),
    "mine-parallel": lambda f, p, cb, cancel: f.mine_parallel_citations(**p, on_progress=cb, cancel_check=cancel),
    "match-legislation": lambda f, p, cb, cancel: f.match_named_legislation(**p, on_progress=cb, cancel_check=cancel),
    "match-echr": lambda f, p, cb, cancel: f.match_echr_reports(**p, on_progress=cb, cancel_check=cancel),
    "rescan": lambda f, p, cb, cancel: f.rescan(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        run_id=p.get("_resume_run_id"), on_progress=cb, cancel_check=cancel),
    "suggest-matches": lambda f, p, cb, cancel: f.suggest_matches(**p, on_progress=cb, cancel_check=cancel),
    "harvest-echr": lambda f, p, cb, cancel: f.harvest_missing_echr(**p, on_progress=cb, cancel_check=cancel),
    "run-watch": lambda f, p, cb, cancel: f.run_watch(watch_id=p["watch_id"], on_progress=cb, cancel_check=cancel),
    # Harvest one source in the background — the "backfill this whole source" action.
    # Long-running by design (a full catalogue walk), so it belongs in the job table
    # rather than a request that has to return.
    "harvest-source": lambda f, p, cb, cancel: f.harvest(
        **p, on_progress=cb, cancel_check=cancel),
    # Finish an interrupted bulk import's resolve/tag phases without re-running
    # discovery or extraction — batched, checkpointed, cancellable.
    "finish-bulk-postprocess": lambda f, p, cb, cancel: f.finish_bulk_postprocess(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
    # Whole-source reparse from stored raw (a parser upgrade reaching held docs) —
    # parallel, progress-reported, cancellable, and resumable from the stable_id cursor.
    "reparse-source": lambda f, p, cb, cancel: f.reparse_source(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
    # Re-anchor drifted citation offsets to a source's current text (the repair for a
    # reparse that regenerated text without re-extraction) — no grammar, no re-resolution;
    # resumable from the stable_id cursor.
    "reanchor-citations": lambda f, p, cb, cancel: f.reanchor_source(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
    "gap-scan": lambda f, p, cb, cancel: f.gap_scan(**p, on_progress=cb, cancel_check=cancel),
    # Decorate held Canadian decisions with CanLII metadata + citator edges —
    # budget-metered, resumable (each checked case is stamped, so a re-run walks on).
    "canlii-enrich": lambda f, p, cb, cancel: f.canlii_enrich(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
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
    def reap_orphans(self, *, auto_resume: bool = False) -> int:
        """Mark this process's leftover 'running' rows as interrupted (called at startup).
        Their worker threads died with the previous process; without this they show as
        live forever."""
        with self.facade._open() as (cat, _rs, _ts):
            rows = [dict(r) for r in cat.running_jobs() if r["origin"] == self.origin]
            n = cat.orphan_running_jobs(self.origin)
            cat.prune_jobs()
        if n:
            log.info("marked %d orphaned %s job(s) as interrupted", n, self.origin)
        if auto_resume:
            for row in rows:
                if row["kind"] in AUTO_RESUME_KINDS and not row["cancel"]:
                    self._resume_row(row)
        return n

    def _max_concurrent(self) -> int:
        """How many jobs run at once — UI-configurable (RAGLEX_MAX_CONCURRENT_JOBS), so a
        busy box can be throttled without a redeploy. Extras queue (see :meth:`start`)."""
        try:
            return max(1, int(os.environ.get("RAGLEX_MAX_CONCURRENT_JOBS") or MAX_CONCURRENT_JOBS))
        except (TypeError, ValueError):
            return MAX_CONCURRENT_JOBS

    def _dedup_hit(self, kind: str, params: dict, pool) -> dict | None:
        """If an identical job (singleton kind, or a DEDUP kind with the same params) is
        already in ``pool`` (running and/or queued), the 'already there' response — so a
        second identical request neither double-runs nor stacks in the queue."""
        if kind in SINGLETON_KINDS and kind not in _SCAN_KINDS:
            for j in pool:
                if j["kind"] == kind:
                    return {"job_id": j["job_id"], "already_running": True}
        elif kind in DEDUP_KINDS:
            want = json.dumps(params, sort_keys=True)
            for j in pool:
                if j["kind"] == kind and json.dumps(
                        json.loads(j["params_json"] or "{}"), sort_keys=True) == want:
                    return {"job_id": j["job_id"], "already_running": True}
        return None

    def _blocked_by_running(self, kind: str, params: dict, running) -> bool:
        """Whether a queued job of ``kind`` must keep waiting because a RUNNING job would
        conflict with it (scan-scope overlap, singleton, or same-params dedup)."""
        if any(_scan_conflict(kind, params, j) for j in running):
            return True
        return self._dedup_hit(kind, params, running) is not None

    def start(self, kind: str, label: str, params: dict | None = None, *,
              resumed_from: str | None = None, root_job_id: str | None = None,
              attempt: int = 1, checkpoint: dict | None = None, queue: bool = False) -> dict:
        """Start a job, or QUEUE it. It runs immediately if a concurrency slot is free and
        ``queue`` is False; otherwise (``queue=True`` — "add to queue" — or the box is at
        ``_max_concurrent``) it's recorded ``queued`` and promoted FIFO as slots free."""
        if kind not in RUNNERS:
            return {"error": f"unknown job kind {kind!r}"}
        # "Pause scheduled jobs" holds the SCHEDULER's own recurring work + due watches only
        # (origin='scheduler'); manual (origin='api') and already-queued jobs still run.
        if self.origin == "scheduler" and scheduler_paused():
            return {"paused": True}
        params = dict(params or {})
        with self.facade._open() as (cat, _rs, _ts):
            running = cat.running_jobs()
            for j in running:
                if _scan_conflict(kind, params, j):
                    return {"job_id": j["job_id"], "already_running": True,
                            "conflict": "citation extraction scope overlaps"}
            # Dedup against running AND already-queued, so a repeat click doesn't stack.
            hit = self._dedup_hit(kind, params, list(running) + list(cat.queued_jobs()))
            if hit is not None:
                return hit
            job_id = uuid.uuid4().hex[:8]
            policy = RESUME_POLICIES.get(kind, "restart")
            root = root_job_id or job_id
            if policy == "checkpoint":
                params["_resume_run_id"] = root
            at_capacity = len(running) >= self._max_concurrent()
            status = "queued" if (queue or at_capacity) else "running"
            cat.create_job(job_id, kind, label, params, origin=self.origin,
                           root_job_id=root, resumed_from=resumed_from,
                           resume_policy=policy, attempt=attempt, checkpoint=checkpoint,
                           status=status)
        if status == "running":
            threading.Thread(target=self._worker, args=(job_id, kind, params), daemon=True).start()
            return {"job_id": job_id}
        return {"job_id": job_id, "queued": True}

    def promote_queued(self) -> list[str]:
        """Start queued jobs (oldest first) up to the concurrency cap, skipping any that
        would conflict with a running job. Called when a slot frees (a job finishes) and on
        every scheduler tick, so promotion survives a crash and works across processes — the
        atomic claim (:meth:`Catalogue.claim_queued_job`) ensures each job starts once."""
        started: list[str] = []
        with self.facade._open() as (cat, _rs, _ts):
            while True:
                running = cat.running_jobs()
                if len(running) >= self._max_concurrent():
                    break
                picked = None
                for q in cat.queued_jobs():
                    p = json.loads(q["params_json"] or "{}")
                    if self._blocked_by_running(q["kind"], p, running):
                        continue
                    if cat.claim_queued_job(q["job_id"]):
                        picked = (q["job_id"], q["kind"], p)
                        break
                if picked is None:
                    break
                jid, k, p = picked
                threading.Thread(target=self._worker, args=(jid, k, p), daemon=True).start()
                started.append(jid)
        return started

    def _worker(self, job_id: str, kind: str, params: dict) -> None:
        state = {"progress": {}, "checkpoint": None, "log": [], "cancel": False,
                 "last_poll": 0.0, "last_write": 0.0, "last_stage": None}
        stopped = threading.Event()

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
            checkpoint = p.pop("_checkpoint", None)
            if checkpoint is not None:
                state["checkpoint"] = checkpoint
            state["progress"] = p
            line = fmt_progress(p)
            if line and (not state["log"] or state["log"][-1] != line):
                state["log"].append(line)
                if len(state["log"]) > 300:
                    del state["log"][:100]
            # The heartbeat is a write; throttle it to ~1/s or a 20k-document loop turns
            # into 20k UPDATEs. A phase transition bypasses the throttle: the switch
            # from "extracting" to "resolving" must be visible immediately, not hidden
            # behind the last extraction line for however long the next phase's first
            # batch takes.
            stage_changed = p.get("stage") != state["last_stage"]
            state["last_stage"] = p.get("stage")
            # Stamp when THIS stage began and its first counter value, so the rate/ETA
            # is computed within the stage. Dividing done by whole-job elapsed showed
            # "~4d left" on a 400-item resolve phase merely because the job had spent
            # hours in earlier phases; a resumed counter (a restored relation cursor)
            # inflated it the other way.
            if stage_changed:
                state["stage_meta"] = {
                    "stage_started_at": _now_iso(),
                    "stage_done0": p.get("done") if isinstance(p.get("done"), (int, float)) else 0,
                }
            p.update(state.get("stage_meta") or {})
            if stage_changed or time.monotonic() - state["last_write"] >= 1.0:
                state["last_write"] = time.monotonic()
                try:
                    with self.facade._open() as (cat, _rs, _ts):
                        cat.heartbeat_job(job_id, p, state["log"], checkpoint=state["checkpoint"])
                except Exception:  # noqa: BLE001
                    pass
            if self.yield_s:
                time.sleep(self.yield_s)  # yield the GIL so the API never starves

        def pulse() -> None:
            """Keep a truthful liveness heartbeat during one long document/SQL phase."""
            while not stopped.wait(30):
                try:
                    with self.facade._open() as (cat, _rs, _ts):
                        cat.pulse_job(job_id)
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=pulse, daemon=True).start()
        try:
            result = RUNNERS[kind](self.facade, params, on_progress, cancel_check)
            status = "cancelled" if state["cancel"] else "done"
            state["log"].append(f"— {status} —")
        except Exception as exc:  # noqa: BLE001 — surface to the poller, don't crash
            result, status = {"error": str(exc)}, "error"
            state["log"].append(f"✗ error: {exc}")
            log.exception("job %s (%s) failed", job_id, kind)
        finally:
            stopped.set()
        try:
            with self.facade._open() as (cat, _rs, _ts):
                # Persist the FINAL progress + checkpoint before closing the row. The
                # throttled heartbeat can be up to a second of events stale, which is
                # how a French bulk import "froze" at 1,737,199/1,737,278 forever: the
                # last 79 documents finished inside the throttle window and their
                # progress was never written.
                if state["progress"]:
                    cat.heartbeat_job(job_id, state["progress"], state["log"],
                                      checkpoint=state["checkpoint"])
                cat.finish_job(job_id, status, result, state["log"])
                finished = dict(cat.get_job(job_id))
        except Exception:  # noqa: BLE001
            log.exception("could not record completion of job %s", job_id)
            finished = None
        if finished and finished.get("restart_requested"):
            self._resume_row(finished)
        # A slot just freed → promote the next queued job(s). Best-effort: the scheduler
        # tick also promotes, so a failure here self-heals within a tick.
        try:
            self.promote_queued()
        except Exception:  # noqa: BLE001
            log.exception("promote_queued after job %s failed", job_id)

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
        lease_idle = _age_seconds(j["lease_heartbeat_at"] or j["heartbeat_at"]) if running else 0.0
        logs = _load(j["log_json"], [])
        progress = _load(j["progress_json"], {})
        # Rate/ETA are computed WITHIN the current stage (its own start time and
        # starting counter, stamped by the worker) — whole-job elapsed made a phase
        # that just started look days long, and a resumed cursor made it look done.
        done, total = progress.get("done"), progress.get("total")
        elapsed = _age_seconds(progress.get("stage_started_at") or j["started_at"])
        done0 = progress.get("stage_done0") if isinstance(progress.get("stage_done0"), (int, float)) else 0
        rate = ((float(done) - float(done0)) / elapsed
                if elapsed > 0 and isinstance(done, (int, float)) and done > done0 else None)
        eta = ((float(total) - float(done)) / rate
               if rate and isinstance(total, (int, float)) and total >= done else None)
        out = {
            "id": j["job_id"], "kind": j["kind"], "label": j["label"], "status": j["status"],
            "origin": j["origin"], "progress": progress,
            "started_at": j["started_at"], "finished_at": j["finished_at"],
            "idle_s": round(idle, 1), "stalled": running and idle >= STALL_SECONDS,
            "process_alive": running and lease_idle < STALL_SECONDS,
            "lease_idle_s": round(lease_idle, 1),
            "rate_per_s": round(rate, 3) if rate else None,
            "eta_s": round(eta) if eta is not None else None,
            "last": (logs or [""])[-1],
            "result": _load(j["result_json"], None),
            "resume": {
                "policy": j["resume_policy"] or "restart",
                "root_job_id": j["root_job_id"] or j["job_id"],
                "resumed_from": j["resumed_from"], "attempt": j["attempt"] or 1,
                "checkpoint": _load(j["checkpoint_json"], {}),
            },
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
            # A queued job hasn't started — drop it outright; a running one gets the
            # cooperative cancel flag its worker polls.
            if cat.cancel_queued_job(job_id):
                return {"job_id": job_id, "cancelled": True, "was_queued": True}
            return {"job_id": job_id, "cancelling": cat.request_job_cancel(job_id)}

    def restart(self, job_id: str) -> dict:
        """Re-launch a job from where its persisted data left off — for a frozen job (the
        host slept and its network socket died) or any finished/cancelled one. Rebuilt from
        the stored (kind, params), so it works even across the restart that lost the
        original process."""
        with self.facade._open() as (cat, _rs, _ts):
            j = cat.get_job(job_id)
            if not j:
                return {"error": "unknown job"}
            if j["status"] == "running":
                # Never overlap an old worker with its replacement. Python cannot kill a
                # thread parked in a socket; marking it finished and launching another
                # caused two writers to race when the old socket eventually returned.
                cat.request_job_restart(job_id)
                return {"job_id": job_id, "cancelling": True,
                        "restart_when_stopped": True}
            row = dict(j)
        return self._resume_row(row)

    def _resume_row(self, row: dict) -> dict:
        import json as _json

        try:
            params = _json.loads(row.get("params_json") or "{}")
            checkpoint = _json.loads(row.get("checkpoint_json") or "{}")
        except (ValueError, TypeError):
            params, checkpoint = {}, {}
        # Some very large catalogues expose a durable discovery cursor. Restore it
        # into the adapter options before relaunching: document deduplication prevents
        # duplicate storage, but without the cursor an NL backfill at offset 930,000
        # still spends many hours walking those 930,000 records again. The cursor is
        # honoured in ANY phase — harvest merges it into its extract/resolve/tag
        # checkpoints precisely so an interruption after discovery doesn't restart
        # the upstream walk from 0.
        if row.get("kind") == "harvest-source":
            if (checkpoint.get("source") == params.get("source")
                    and checkpoint.get("resume_offset") is not None):
                options = dict(params.get("options") or {})
                options["start_offset"] = int(checkpoint["resume_offset"])
                params["options"] = options
            # An interrupted bulk resolve phase left a committed relation-id cursor;
            # restore it so the resumed job continues the range walk instead of
            # rescanning already-resolved ranges.
            if (checkpoint.get("phase") == "resolve"
                    and checkpoint.get("relation_id") is not None):
                params["postprocess_after_relation_id"] = int(checkpoint["relation_id"])
        elif row.get("kind") == "finish-bulk-postprocess":
            phase = checkpoint.get("phase")
            if phase == "resolve" and checkpoint.get("relation_id") is not None:
                params["after_relation_id"] = int(checkpoint["relation_id"])
            elif phase == "tag":
                # resolution completed before the interruption — don't redo it, and
                # continue tagging from the persisted absolute position.
                params["resolve"] = False
                if checkpoint.get("completed") is not None:
                    params["tag_start"] = int(checkpoint["completed"])
        # A whole-source reparse / re-anchor continues from the last stable_id it committed.
        if (row.get("kind") in ("reparse-source", "reanchor-citations")
                and checkpoint.get("after_stable_id")
                and checkpoint.get("source") == params.get("source")):
            params["after_stable_id"] = checkpoint["after_stable_id"]
        root = row.get("root_job_id") or row["job_id"]
        res = self.start(row["kind"], row["label"], params,
                         resumed_from=row["job_id"], root_job_id=root,
                         attempt=int(row.get("attempt") or 1) + 1,
                         checkpoint=checkpoint)
        res["restarted_from"] = row["job_id"]
        res["resume_policy"] = RESUME_POLICIES.get(row["kind"], "restart")
        return res
