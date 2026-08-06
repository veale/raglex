"""Background jobs (§8) — durable, cross-process, restartable.

Long operations (drain the worklist, re-scan the corpus, run a watch) run in a
thread and report progress so the UI can show "fetching 5/30" instead of blocking on one
request. The registry backing them used to be a dict in the API process, which cost three
things worth having:

- **durability** — a deploy or crash erased a running job's history mid-run;
- **restartability** — a frozen job (its socket died when the host slept) could only be
  relaunched from the closure held in memory, which died with the process;
- **visibility** — the scheduler runs in a *different container*, so its own work never
  appeared in the jobs panel at all. That is precisely why a drain silently storing
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
# so these stay one-at-a-time. Everything else (harvest a category, expand-citing) is
# keyed to a specific input and may run simultaneously.
SINGLETON_KINDS = frozenset({
    "rescan-citations", "backfill-metadata", "backfill-edge-keys", "repair-au-cth",
    "repair-de-citations", "repair-de-renditions", "repair-eu-repeals",
    "repair-eu-annexes",
    "backfill-eu-stubs",
    "rebuild-citation-counts", "rebuild-authority", "match-reports",
    "rescan", "mine-parallel", "match-legislation", "match-echr", "harvest-echr",
    "rescan-matching",
    "suggest-matches", "classify-guidance",
    # one relation-range cursor over the whole graph — two would double-resolve ranges
    "finish-bulk-postprocess",
    # one metered walk of the Canadian enrichment queue — two would double-spend
    # the CanLII budget on the same head-of-queue documents
    "canlii-enrich",
    # only ever one indexing pass: two would race over the same pending_embedding queue
    "embed",
    # one Myriad relay at a time — two would ship/submit/import over the same shard dir
    "hpc-embed",
    # the whole point is serial: one maintenance pass at a time, never competing with itself
    "maintenance-run",
})
MAX_CONCURRENT_JOBS = 6
# Keyed jobs deduped by (kind, params): don't start an IDENTICAL one while it's in flight,
# but different-parameter runs proceed. harvest-all is here (not a blanket singleton): each
# click targets ONE adapter (a corpus-map category) and drains a disjoint candidate set, so
# a us-caselaw harvest must not be blocked by a running uk-legislation one. Two clicks of the
# SAME category still dedup, and the nightly whole-queue drain (no adapter → distinct params)
# dedups against a second whole-queue drain but no longer blocks the per-category buttons.
DEDUP_KINDS = frozenset({
    "run-watch", "gap-scan", "harvest-source", "harvest-all", "static-export",
    "static-bundle", "sync-eu-consolidations",
})

# Jobs that must never sit behind a full queue. A static export is started by someone
# waiting at the browser for the file (or by the schedule that keeps the published folder
# current); making it queue behind a six-hour import means the download never arrives.
# They're read-only passes over the corpus, so running one over the concurrency cap costs
# little. Dedup still applies — a second click of the same export joins the first.
#
# A consolidation import is the same shape of work for the same reason: it is triggered by
# a reader OPENING an EU act whose dated versions are absent, and it decides which text
# that page shows. Queued behind a multi-hour harvest it arrives long after the reader has
# gone, and the act keeps serving its un-consolidated text until then. It fetches one
# act's expressions from Cellar — small, bounded, and mostly deduped before any download.
QUEUE_EXEMPT_KINDS = frozenset({
    "static-export", "static-bundle", "sync-eu-consolidations",
})

# Resume is an explicit contract, not a blanket promise that "idempotent" means no
# repeated work. ``checkpoint`` jobs stamp each completed document with a stable root
# run id. ``deduplicate`` jobs restart discovery but cheaply skip durable outputs.
# ``restart`` jobs are safe to run again but have no useful mid-phase cursor.
RESUME_POLICIES = {
    "rescan-citations": "checkpoint", "rescan": "checkpoint",
    "harvest-source": "deduplicate", "harvest-all": "deduplicate",
    "sync-eu-consolidations": "deduplicate",
    "embed": "deduplicate",
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
    # resumes the local Formex package repair from the last completely reparsed/re-mined act
    "repair-eu-annexes": "checkpoint",
    # re-derives its scope from the edges each time, then skips what the run already
    # stamped — so a restart re-reads only what it hasn't reached
    "rescan-contested-shorthands": "checkpoint",
}
AUTO_RESUME_KINDS = frozenset(RESUME_POLICIES)
# These all write the citations table; a re-anchor and a rescan of the SAME source must
# not run at once (they'd race the same offsets), but disjoint sources may.
#
# ``reparse-source`` belongs here for a stronger reason than the rest: it rewrites the
# document TEXT the offsets are measured against. Two of them ran concurrently over all
# 100,027 uk-legislation documents — one auto-resumed by reap_orphans at API startup, the
# other promoted off the queue two seconds later — and re-anchored the same citations
# against text the other was replacing (1.79M offsets vs 123k for the same document set).
# It was in none of the three guards, so nothing stopped it.
_SCAN_KINDS = frozenset({"rescan-citations", "rescan", "reanchor-citations",
                         "rescan-matching", "rescan-contested-shorthands",
                         "reparse-source"})


# Kinds that GROW the corpus (new documents/edges) and therefore stale the derived layers —
# the embedding index, the citation-count roll-up, and the authority/PageRank scores. After
# one of these finishes, ``_chain_postprocess`` refreshes those layers. The follow-ups
# themselves are excluded, so there is no rebuild→rebuild loop.
CHAIN_TRIGGER_KINDS = frozenset({
    "harvest-source", "harvest-all", "expand-citing",
    "refresh-category", "pull-ag-opinions", "harvest-echr", "canlii-enrich",
    "import-bailii-corpus", "import-bailii-zip", "import-bailii-dir", "import-bailii-parquet",
    "import-indian-sci", "import-sg-seed", "import-westlaw-zip", "import-westlaw-dir",
    "import-caselaw-zip", "import-caselaw-dir", "reparse-source", "finish-bulk-postprocess",
    # a phantom prune changes the counts as surely as a harvest does
    "repair-de-citations",
    "repair-eu-annexes", "sync-eu-consolidations",
})
# (follow-up kind, min seconds since its last completion before re-running). embed is cheap
# and incremental, so it stays in the chain: a document with no vector is genuinely absent
# from semantic search until it has one.
#
# The two ROLL-UPS are not in the chain by default. Both are whole-graph walks, both are
# already scheduled tasks ("counts", "authority" — weekly, 04:00), and neither affects
# correctness: a freshly harvested document is found, read and cited perfectly well while
# carrying a stale authority score. Chaining them off every harvest meant up to ~48 PageRank
# walks a day over a 17M-edge graph to keep a RANKING aggregate slightly fresher, which is
# most of what made a continuously-harvesting box feel sluggish. Turn the "postprocess-
# rollups" scheduler task on to restore the old behaviour.
_COOLDOWN_SCALE = float(os.environ.get("RAGLEX_POSTPROCESS_COOLDOWN_S") or 0) or None
CHAIN_FOLLOWUPS = (
    ("embed", _COOLDOWN_SCALE or 300.0),
)
CHAIN_ROLLUP_FOLLOWUPS = (
    ("rebuild-citation-counts", _COOLDOWN_SCALE or 1200.0),
    ("rebuild-authority", _COOLDOWN_SCALE or 1800.0),
)
# Cap on the chained (auto) embed batch so one post-harvest follow-up can never pull the
# whole multi-million-row embedding queue into memory (it drains across harvests instead).
_AUTOEMBED_CHAIN_BATCH = int(os.environ.get("RAGLEX_AUTOEMBED_CHAIN_BATCH") or 2000)
# A job that keeps dying gets auto-resumed under attempt+1 forever. If the death is
# deterministic (an OOM on this box, a poison document), that is an infinite crash-restart
# loop that thrashes the whole host. Stop auto-resuming past this many attempts; a human can
# still restart it explicitly. 0 disables the cap.
MAX_RESUME_ATTEMPTS = int(os.environ.get("RAGLEX_MAX_RESUME_ATTEMPTS") or 8)


def _parse_iso(ts: str) -> float:
    """Epoch seconds from a stored ISO timestamp; 0 (→ "long ago") if unparseable."""
    if not ts:
        return 0.0
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _resume_exhausted(row: dict) -> bool:
    """True when a job has already been auto-resumed too many times — the guard against an
    infinite crash→resume loop (a deterministic OOM/poison-doc) hammering the host. The
    attempt counter increments on each resume (see _resume_row)."""
    if MAX_RESUME_ATTEMPTS <= 0:
        return False
    try:
        attempt = int(row.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    if attempt >= MAX_RESUME_ATTEMPTS:
        log.warning("job %s (%s) hit resume cap (attempt %d ≥ %d) — not auto-resuming; "
                    "restart it manually once the cause is fixed",
                    row.get("job_id"), row.get("kind"), attempt, MAX_RESUME_ATTEMPTS)
        return True
    return False


def _job_did_work(result: dict | None) -> bool:
    """Whether a completed job produced net-new corpus work worth re-deriving layers for.
    Conservative: unrecognised result shapes count as work (the cool-down still bounds churn);
    only an explicit all-zero on the known counters is treated as a no-op."""
    if not isinstance(result, dict):
        return True
    keys = ("stored", "imported", "new", "added", "documents", "fetched", "harvested",
            "resolved", "count", "enriched", "reparsed", "updated")
    seen = [result.get(k) for k in keys if isinstance(result.get(k), (int, float))]
    if not seen:
        return True
    return any(v > 0 for v in seen)


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


# One harvest per SOURCE. Dedup alone does not achieve this: it keys on the exact params,
# so a resumed harvest (which carries resume_unfinished + a discovery cursor) and a fresh
# one of the same source look like different jobs and both run. That happened three times
# in one day on uk-parl-committees — twice from a deploy interrupting a backfill that
# auto-resumed beside its replacement — with both walking the same catalogue, competing
# for the same browser and asking the same API for the same pages.
_SOURCE_EXCLUSIVE_KINDS = frozenset({"harvest-source"})


def _source_conflict(kind: str, params: dict, running_row) -> bool:
    """Whether a harvest would overlap a running harvest of the same source."""
    if kind not in _SOURCE_EXCLUSIVE_KINDS or running_row["kind"] not in _SOURCE_EXCLUSIVE_KINDS:
        return False
    import json as _json
    try:
        other = _json.loads(running_row["params_json"] or "{}")
    except (ValueError, TypeError):
        other = {}
    a = str(params.get("source") or "*")
    b = str(other.get("source") or "*")
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


def _now_iso_offset(delta_s: float) -> str:
    """ISO timestamp ``delta_s`` seconds from now (negative = in the past), for staleness
    cutoffs compared against the stored ISO lease heartbeats."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


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
        marker = {
            "transient": "↻",       # exhausted retries now; remains eligible later
            "rate_limited": "⏸",    # source queue paused, not an item failure
            "absent": "∅",          # upstream positively says no such item
        }.get(p.get("outcome"), "✓" if p["ok"] else "✗")
        parts.append(marker)
    if p.get("msg"):
        parts.append(str(p["msg"]))
    return "  ".join(parts).strip()


# kind → the facade call it names. Persisting (kind, params) instead of a closure is what
# makes a job survive the process that started it.
def _hpc_embed(facade, params, on_progress, cancel_check):
    from .hpc_orchestrator import run_hpc_embed
    return run_hpc_embed(facade, params, on_progress, cancel_check)


def _maintenance(facade, params, on_progress, cancel_check):
    from .maintenance import run_maintenance
    return run_maintenance(facade, params, on_progress, cancel_check)


def _static_export(facade, params, on_progress, cancel_check):
    from .static_export import build_static_export_cache

    return build_static_export_cache(
        facade,
        params["stable_id"],
        max_snippets=int(params.get("max_snippets") or 4),
        on_progress=on_progress,
        cancel_check=cancel_check,
    )


def _static_bundle(facade, params, on_progress, cancel_check):
    from .static_bundle import build_bundle

    return build_bundle(facade, params, on_progress, cancel_check)


RUNNERS: dict[str, Callable] = {
    "static-export": _static_export,
    "static-bundle": _static_bundle,
    # Re-extract everything whose TEXT matches a query. The scope a citation fix needs:
    # when a grammar or shorthand changes, the documents to re-read are the ones that
    # MENTION the thing, which is exactly what the edges do not yet record.
    "rescan-matching": lambda f, p, cb, cancel: f.rescan_matching(
        query=p.get("query") or "", exact=bool(p.get("exact", True)),
        limit=int(p.get("limit") or 20000), on_progress=cb, cancel_check=cancel),
    "rescan-citations": lambda f, p, cb, cancel: f.apply_rules(
        source=p.get("source"), sources=p.get("sources"),
        source_prefix=p.get("source_prefix"), target_ids=p.get("target_ids"),
        document_ids=p.get("document_ids"),
        run_id=p.get("_resume_run_id"),
        on_progress=cb, cancel_check=cancel),
    "backfill-metadata": lambda f, p, cb, cancel: f.backfill_document_metadata(on_progress=cb),
    "backfill-edge-keys": lambda f, p, cb, cancel: f.backfill_edge_keys(on_progress=cb, cancel_check=cancel),
    # re-fetch EU instruments stored as bare metadata stubs (a transient harvest
    # failure left ~7,400 heavily-cited acts as dead ends)
    "backfill-eu-stubs": lambda f, p, cb, cancel: f.backfill_eu_stubs(
        limit=int(p.get("limit") or 500), on_progress=cb, cancel_check=cancel),
    "rebuild-citation-counts": lambda f, p, cb, cancel: f.rebuild_citation_counts(on_progress=cb),
    "rebuild-authority": lambda f, p, cb, cancel: f.rebuild_authority(on_progress=cb, cancel_check=cancel),
    "pull-ag-opinions": lambda f, p, cb, cancel: f.pull_ag_opinions(on_progress=cb, cancel_check=cancel),
    "harvest-all": lambda f, p, cb, cancel: f.harvest_all_references(**p, on_progress=cb, cancel_check=cancel),
    "expand-citing": lambda f, p, cb, cancel: f.expand_citing_cases(**p, on_progress=cb, cancel_check=cancel),
    "refresh-category": lambda f, p, cb, cancel: f.refresh_category(**p, on_progress=cb, cancel_check=cancel),
    "match-reports": lambda f, p, cb, cancel: f.match_report_citations(on_progress=cb, cancel_check=cancel),
    "import-bailii-corpus": lambda f, p, cb, cancel: f.import_bailii_corpus(**p, on_progress=cb, cancel_check=cancel),
    "import-bailii-zip": lambda f, p, cb, cancel: f.import_bailii_zip(**p, on_progress=cb, cancel_check=cancel),
    "import-bailii-dir": lambda f, p, cb, cancel: f.import_bailii_dir(**p, on_progress=cb, cancel_check=cancel),
    "import-bailii-parquet": lambda f, p, cb, cancel: f.import_bailii_parquet(**p, on_progress=cb, cancel_check=cancel),
    "import-indian-sci": lambda f, p, cb, cancel: f.import_indian_sci(**p, on_progress=cb, cancel_check=cancel),
    "import-sg-seed": lambda f, p, cb, cancel: f.import_sg_seed(**p, on_progress=cb, cancel_check=cancel),
    "repair-au-cth": lambda f, p, cb, cancel: f.repair_au_cth(**p, on_progress=cb, cancel_check=cancel),
    "backfill-eu-case-names": lambda f, p, cb, cancel: f.backfill_titles(**p, on_progress=cb, cancel_check=cancel),
    "backfill-ag-names": lambda f, p, cb, cancel: f.backfill_ag_names(**p, on_progress=cb, cancel_check=cancel),
    "repair-mojibake": lambda f, p, cb, cancel: f.repair_mojibake(**p, on_progress=cb, cancel_check=cancel),
    # re-validate German citations against the current grammar (drops what it would no
    # longer mint) — the standing migration for a German grammar fix
    "repair-de-citations": lambda f, p, cb, cancel: f.repair_de_citations(**p, on_progress=cb, cancel_check=cancel),
    # fold a second register's copies of judgments we already hold back into the originals
    "repair-de-renditions": lambda f, p, cb, cancel: f.repair_de_duplicate_renditions(**p, on_progress=cb, cancel_check=cancel),
    # re-ask CELLAR which "repeals" edges were only implicit ones
    "repair-eu-repeals": lambda f, p, cb, cancel: f.repair_eu_implicit_repeals(**p, on_progress=cb, cancel_check=cancel),
    "repair-eu-annexes": lambda f, p, cb, cancel: f.repair_eu_split_annexes(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
    "sync-eu-consolidations": lambda f, p, cb, cancel: f.sync_eu_consolidations(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
    "backfill-intituling": lambda f, p, cb, cancel: f.backfill_intituling(**p, on_progress=cb, cancel_check=cancel),
    "resegment-judgments": lambda f, p, cb, cancel: f.resegment_judgments(**p, on_progress=cb, cancel_check=cancel),
    "build-fts": lambda f, p, cb, cancel: f.build_freetext_index(**p, on_progress=cb, cancel_check=cancel),
    "repair-fts-positions": lambda f, p, cb, cancel: f.repair_freetext_positions(
        **p, on_progress=cb, cancel_check=cancel),
    "localise-text": lambda f, p, cb, cancel: f.localise_text(**p, on_progress=cb, cancel_check=cancel),
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
    # Re-derive held CN/TN notices from their raw: the structured form, the docketed
    # title, and their citations (which were mined out of the old flat parse's text).
    # Re-fetch notices stored as the OJ issue's masthead instead of the notice itself
    # (their raw is the wrapper, so no local reparse can recover them).
    "repair-oj-wrapper-notices": lambda f, p, cb, cancel: f.repair_oj_wrapper_notices(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
    # Re-read the documents that carry an edge from a CONTESTED learned shorthand — a
    # name the store holds against several candidates, which it used to apply on the
    # coincidence that the document cited one of them ("PACE" → RIPA). The rule is
    # fixed; this clears what it already wrote.
    "rescan-contested-shorthands": lambda f, p, cb, cancel: f.rescan_contested_shorthands(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        run_id=p.get("_resume_run_id"), on_progress=cb, cancel_check=cancel),
    "reparse-pending-notices": lambda f, p, cb, cancel: f.reparse_pending_eu_notices(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
    # Re-anchor drifted citation offsets to a source's current text (the repair for a
    # reparse that regenerated text without re-extraction) — no grammar, no re-resolution;
    # resumable from the stable_id cursor.
    "reanchor-citations": lambda f, p, cb, cancel: f.reanchor_source(
        **{k: v for k, v in p.items() if not k.startswith("_")},
        on_progress=cb, cancel_check=cancel),
    "gap-scan": lambda f, p, cb, cancel: f.gap_scan(**p, on_progress=cb, cancel_check=cancel),
    # Harvest EU acts' act-to-act CDM relationships (repeals/amends/corrects/legal-basis) so
    # old directives learn they were repealed/recast — the legislative-status graph.
    "enrich-eu-legislation": lambda f, p, cb, cancel: f.enrich_eu_legislation(
        **{k: v for k, v in p.items() if not k.startswith("_")}, on_progress=cb, cancel_check=cancel),
    # Drive the whole UCL-Myriad bulk-embed relay (export→ship→qsub→poll→fetch→import) as one
    # resumable, queue-aware, deadline-guarded job. Dry-run unless params has go=True.
    "hpc-embed": lambda f, p, cb, cancel: _hpc_embed(f, p, cb, cancel),
    # Serial DB maintenance + safe repair pass — diagnoses and works a queue of repairs/
    # rescans/roll-ups ONE at a time (never over-parallelises), no LLM. See maintenance.py.
    "maintenance-run": lambda f, p, cb, cancel: _maintenance(f, p, cb, cancel),
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
        # Throttle state for poll() (the UI polls the queue every few seconds).
        self._promote_lock = threading.Lock()
        self._last_promote = 0.0

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
                if (row["kind"] in AUTO_RESUME_KINDS and not row["cancel"]
                        and not _resume_exhausted(row)):
                    self._resume_row(row)
        return n

    def reap_stalled(self, *, auto_resume: bool = True) -> int:
        """Reap jobs whose owning process stopped pulsing (a slept/woke or crashed container)
        without waiting for that container to restart, and prune the finished-job backlog so
        the table (and the Jobs panel / worklist that scan it) stays fast. Safe to call from
        any process on a cadence."""
        cutoff = _now_iso_offset(-max(STALL_SECONDS * 5, 600.0))
        with self.facade._open() as (cat, _rs, _ts):
            rows = [dict(r) for r in cat.reap_stalled_jobs(cutoff)]
            cat.prune_jobs()
        if rows:
            log.info("reaped %d stalled job(s): %s", len(rows),
                     ", ".join(sorted({r["kind"] for r in rows})))
        if auto_resume:
            for row in rows:
                if (row["kind"] in AUTO_RESUME_KINDS and not row.get("cancel")
                        and not _resume_exhausted(row)):
                    try:
                        self._resume_row(row)
                    except Exception:  # noqa: BLE001
                        log.exception("could not resume reaped job %s", row.get("job_id"))
        return len(rows)

    def _max_concurrent(self) -> int:
        """How many jobs run at once — UI-configurable (RAGLEX_MAX_CONCURRENT_JOBS), so a
        busy box can be throttled without a redeploy. Extras queue (see :meth:`start`)."""
        try:
            return max(1, int(os.environ.get("RAGLEX_MAX_CONCURRENT_JOBS") or MAX_CONCURRENT_JOBS))
        except (TypeError, ValueError):
            return MAX_CONCURRENT_JOBS

    @staticmethod
    def _slots_used(running) -> int:
        """How many of the concurrency slots the running jobs actually occupy.

        Queue-exempt kinds are *deliberately* allowed to run over the cap (a reader is
        waiting on them), so counting them against it inverts the exemption: four
        reader-triggered consolidation imports would pin the box at capacity and every
        queued harvest, watch and reparse would wait behind work that was never supposed
        to take a slot. They run beside the queue, not in it."""
        return sum(1 for j in running if j["kind"] not in QUEUE_EXEMPT_KINDS)

    def _dedup_hit(self, kind: str, params: dict, pool) -> dict | None:
        """If an identical job (singleton kind, or a DEDUP kind with the same params) is
        already in ``pool`` (running and/or queued), the 'already there' response — so a
        second identical request neither double-runs nor stacks in the queue."""
        # ``state`` distinguishes a job that is RUNNING from one merely QUEUED — the pool
        # holds both, and callers were reporting every hit as "still running", which
        # described a job that had not started as one that would not finish.
        if kind in SINGLETON_KINDS and kind not in _SCAN_KINDS:
            for j in pool:
                if j["kind"] == kind:
                    return {"job_id": j["job_id"], "already_running": True,
                            "state": j["status"]}
        elif kind in DEDUP_KINDS:
            want = json.dumps(params, sort_keys=True)
            for j in pool:
                if j["kind"] == kind and json.dumps(
                        json.loads(j["params_json"] or "{}"), sort_keys=True) == want:
                    return {"job_id": j["job_id"], "already_running": True,
                            "state": j["status"]}
        return None

    def _blocked_by_running(self, kind: str, params: dict, running) -> bool:
        """Whether a queued job of ``kind`` must keep waiting because a RUNNING job would
        conflict with it (scan-scope overlap, singleton, or same-params dedup)."""
        if any(_scan_conflict(kind, params, j) for j in running):
            return True
        if any(_source_conflict(kind, params, j) for j in running):
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
                if _source_conflict(kind, params, j):
                    return {"job_id": j["job_id"], "already_running": True,
                            "conflict": "a harvest of this source is already running"}
            # Dedup against running AND already-queued, so a repeat click doesn't stack.
            hit = self._dedup_hit(kind, params, list(running) + list(cat.queued_jobs()))
            if hit is not None:
                return hit
            job_id = uuid.uuid4().hex[:8]
            policy = RESUME_POLICIES.get(kind, "restart")
            root = root_job_id or job_id
            if policy == "checkpoint":
                params["_resume_run_id"] = root
            at_capacity = (self._slots_used(running) >= self._max_concurrent()
                           and kind not in QUEUE_EXEMPT_KINDS)
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
        with self.facade._open() as (cat, _rs, _ts):
            return self._promote_with(cat)

    def _promote_with(self, cat) -> list[str]:
        """The promotion loop against an ALREADY-OPEN catalogue, so a caller that has one
        does not take a second connection from the pool to do this."""
        started: list[str] = []
        blocked: list[str] = []
        cap = self._max_concurrent()
        running: list = []
        while True:
            running = list(cat.running_jobs())
            if self._slots_used(running) >= cap:
                break
            picked = None
            for q in cat.queued_jobs():
                p = json.loads(q["params_json"] or "{}")
                if self._blocked_by_running(q["kind"], p, running):
                    blocked.append(f"{q['job_id']}({q['kind']})")
                    continue
                if cat.claim_queued_job(q["job_id"]):
                    picked = (q["job_id"], q["kind"], p)
                    break
            if picked is None:
                break
            jid, k, p = picked
            threading.Thread(target=self._worker, args=(jid, k, p), daemon=True).start()
            started.append(jid)
        if started:
            log.info("promoted %d queued job(s) into free slots: %s",
                     len(started), ", ".join(started))
        elif blocked:
            # A queue that cannot move is worth a line. It was silent before, which is why
            # a scheduler stuck on a stale max-concurrent sat on a free slot for hours with
            # nothing anywhere saying so.
            log.info("queue not advancing: %d/%d slots used, %d queued job(s) blocked by a "
                     "running conflict (%s)", self._slots_used(running), cap,
                     len(blocked), ", ".join(sorted(set(blocked))[:5]))
        return started

    def poll(self, *, min_interval: float = 5.0) -> dict:
        """Read the queue's state and, at most every ``min_interval``, advance it — on ONE
        connection.

        This is what the Jobs panel polls. Promotion otherwise happens only when a job
        finishes in this process or on the scheduler's tick, which is 900s in the deployed
        compose, so a slot freed anywhere else sat empty for up to a quarter of an hour.
        Doing it here is right; doing it in its own ``_open()`` was not. The endpoint then
        took TWO pool connections in sequence on a path polled every few seconds, and under
        load the second one waited out the pool's 30s timeout and raised — which is exactly
        what it did in production (PoolTimeout in maybe_promote, 07:21). One connection
        does both, and a pool that is momentarily busy costs a beat of promotion rather
        than an exception."""
        return self._queue_state(promote=self._promotion_due(min_interval))

    def queue_state(self) -> dict:
        return self._queue_state(promote=False)

    def _queue_state(self, *, promote: bool) -> dict:
        """What the queue is doing and, when it is not moving, why — the read behind the
        UI's slot counter. ``blocked`` names the queued jobs a running job conflicts with,
        so "3 queued, a slot free, nothing starting" is explained rather than mysterious."""
        cap = self._max_concurrent()
        with self.facade._open() as (cat, _rs, _ts):
            if promote:
                try:
                    self._promote_with(cat)
                except Exception:  # noqa: BLE001 — a poll must never 500 on queue upkeep
                    log.warning("promotion during queue poll failed", exc_info=True)
            running = list(cat.running_jobs())
            queued = list(cat.queued_jobs())
            blocked = [q["job_id"] for q in queued
                       if self._blocked_by_running(
                           q["kind"], json.loads(q["params_json"] or "{}"), running)]
        used = self._slots_used(running)
        return {
            "running": len(running), "queued": len(queued),
            "slots_used": used, "max_concurrent": cap,
            "over_cap": len(running) - used,  # queue-exempt jobs running beside the queue
            "blocked": blocked,
            "scheduler_paused": scheduler_paused(),
        }

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
            # Yield the GIL so the API never starves. When interactive-priority is on
            # (default), this yield point also PARKS the worker while a user is actively
            # reading (UI/MCP): a heavy job's disk IO otherwise evicts the buffer-cache
            # pages an interactive citator view needs, turning a sub-second document open
            # into tens of seconds. Bounded, so backlog work still drains under sustained
            # use; falls back to the plain tiny GIL-yield when nobody is active.
            from . import interactive
            if not interactive.throttle_for_interactive(cancel_check) and self.yield_s:
                time.sleep(self.yield_s)

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
            # Universal first event: even a runner that has not yet reached its own
            # first callback is visibly alive and names what is starting.
            on_progress(stage=f"starting {kind}", done=0)
            result = RUNNERS[kind](self.facade, params, on_progress, cancel_check)
            status = "cancelled" if state["cancel"] else "done"
            state["log"].append(f"— {status} —")
        except Exception as exc:  # noqa: BLE001 — surface to the poller, don't crash
            result, status = {"error": str(exc)}, "error"
            state["log"].append(f"✗ error: {exc}")
            log.exception("job %s (%s) failed", job_id, kind)
            # …and into the review queue, where user feedback and refinement flags go, so
            # a job that fails every night is a work item rather than a log line (§8).
            from .ops.errorlog import report_job_failure
            report_job_failure(self.facade, kind=kind, error=str(exc), job_id=job_id)
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
        # A corpus-growing job just finished → refresh the derived layers (embed index,
        # citation-count roll-up, authority/PageRank) so search + ranking stay current
        # without anyone remembering to click "rebuild". Debounced so a busy import doesn't
        # rebuild PageRank on every batch.
        if status == "done":
            try:
                self._chain_postprocess(kind, result)
            except Exception:  # noqa: BLE001
                log.exception("post-process chaining after job %s failed", job_id)
        # A slot just freed → promote the next queued job(s). Best-effort: the scheduler
        # tick also promotes, so a failure here self-heals within a tick.
        try:
            self.promote_queued()
        except Exception:  # noqa: BLE001
            log.exception("promote_queued after job %s failed", job_id)

    def _chain_postprocess(self, kind: str, result: dict | None) -> None:
        """After a corpus-growing job, enqueue the derived-layer refreshes it invalidates.

        Each follow-up is a singleton (so duplicates coalesce) and time-debounced against its
        own last completion, so continuous harvesting refreshes PageRank periodically rather
        than back-to-back. Skipped when the trigger job did no net work."""
        if kind not in CHAIN_TRIGGER_KINDS or not _job_did_work(result):
            return
        from . import schedule as _schedule
        # Defer the (expensive) roll-ups while a BATCH of corpus-growing work is still in
        # flight — importing 11 Westlaw zips must rebuild the citation counts + PageRank ONCE
        # at the end, not after every zip (each rebuild is invalidated by the next import and
        # the walk is corpus-wide). If any other trigger-kind job is still running or queued,
        # skip: whichever one finishes last sees an empty pipeline and fires the chain then.
        # (The follow-up singleton guard below still coalesces a rare simultaneous-finish.)
        try:
            with self.facade._open() as (cat, _rs, _ts):
                pending = [j for j in (list(cat.running_jobs()) + list(cat.queued_jobs()))
                           if j["kind"] in CHAIN_TRIGGER_KINDS]
            if pending:
                log.info("post-process chain deferred: %d trigger job(s) still pending "
                         "(rollups run once the batch drains)", len(pending))
                return
        except Exception:  # noqa: BLE001 — if the check fails, fall through and chain anyway
            log.exception("could not check pending trigger jobs; chaining regardless")
        now = time.time()
        followups = CHAIN_FOLLOWUPS
        if _schedule.is_enabled("postprocess-rollups"):
            followups = (*followups, *CHAIN_ROLLUP_FOLLOWUPS)
        for follow, cooldown in followups:
            try:
                # The post-harvest embed follow-up is the ONE unbounded, unscheduled path
                # that could re-embed the whole corpus. Gate it on the same 'auto-embed'
                # toggle the scheduler tick honours, so turning auto-embed off is a real,
                # total off switch (it wasn't — this chain ignored it and OOM-looped).
                if follow == "embed" and not _schedule.is_enabled("auto-embed"):
                    continue
                with self.facade._open() as (cat, _rs, _ts):
                    if cat.running_jobs(follow) or any(
                            q["kind"] == follow for q in cat.queued_jobs()):
                        continue  # already pending → it'll pick up the new work
                    recent = cat.recent_job_results(follow, limit=1)
                    last = recent[0]["finished_at"] if recent else None
                if last and (now - _parse_iso(last)) < cooldown:
                    continue  # ran recently enough
                # Bound the chained embed so a single follow-up can never materialise the
                # whole multi-million-row queue in memory; it drains incrementally across
                # successive harvests. Other follow-ups take no params.
                params = {"limit": _AUTOEMBED_CHAIN_BATCH} if follow == "embed" else {}
                self.start(follow, f"auto: {follow} after {kind}", params, queue=True)
            except Exception:  # noqa: BLE001 — one follow-up failing must not block others
                log.exception("could not chain %s", follow)

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
        process_alive = running and lease_idle < STALL_SECONDS
        progress_quiet = running and idle >= STALL_SECONDS
        activity_state = (
            "stopped" if running and not process_alive else
            "waiting" if process_alive and progress_quiet else
            "working" if running else
            "finished"
        )
        out = {
            "id": j["job_id"], "kind": j["kind"], "label": j["label"], "status": j["status"],
            "origin": j["origin"], "progress": progress,
            "started_at": j["started_at"], "finished_at": j["finished_at"],
            "idle_s": round(idle, 1),
            "stalled": running and not process_alive,
            "waiting": process_alive and progress_quiet,
            "activity_state": activity_state,
            "process_alive": process_alive,
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

    def _promotion_due(self, min_interval: float) -> bool:
        now = time.monotonic()
        with self._promote_lock:
            if now - self._last_promote < min_interval:
                return False
            self._last_promote = now
            return True

    def list(self, *, limit: int = 60, promote: bool = True,
             min_interval: float = 5.0) -> list[dict]:
        """The Jobs panel's read — and, throttled, the thing that keeps the queue moving.

        Promotion was hooked onto the queue-status endpoint only, which just the Maintain
        page polls. The jobs DOCK — the panel actually watched while work runs — polls
        this one, so on every other screen a freed slot sat empty until the scheduler's
        900s tick: "Waiting for a slot — 2 queued" with two slots free and nothing
        blocking. Same connection, same throttle as the other poll."""
        due = promote and self._promotion_due(min_interval)
        with self.facade._open() as (cat, _rs, _ts):
            if due:
                try:
                    self._promote_with(cat)
                except Exception:  # noqa: BLE001 — a poll must never 500 on queue upkeep
                    log.warning("promotion during jobs poll failed", exc_info=True)
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
            # A deploy may interrupt AFTER discovery/storage while citation extraction is
            # only part-way through. Re-discovery can legitimately yield zero because the
            # completed backfill frontier is already recorded; explicitly rebuild the
            # extraction worklist from documents whose durable completion stamp is NULL.
            # Without this, attempt 2 says "done — discovered 0" while hundreds of stored
            # documents remain unextracted (the CMA guidance backfill incident).
            params["resume_unfinished"] = True
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
        if (row.get("kind") in ("reparse-source", "reanchor-citations", "repair-eu-annexes")
                and checkpoint.get("after_stable_id")
                and (row.get("kind") == "repair-eu-annexes"
                     or checkpoint.get("source") == params.get("source"))):
            params["after_stable_id"] = checkpoint["after_stable_id"]
        root = row.get("root_job_id") or row["job_id"]
        res = self.start(row["kind"], row["label"], params,
                         resumed_from=row["job_id"], root_job_id=root,
                         attempt=int(row.get("attempt") or 1) + 1,
                         checkpoint=checkpoint)
        res["restarted_from"] = row["job_id"]
        res["resume_policy"] = RESUME_POLICIES.get(row["kind"], "restart")
        return res
