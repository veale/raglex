"""Serial database-maintenance + repair job (§8) — one task at a time, no LLM.

A single durable job that DIAGNOSES the corpus and then works through a queue of maintenance
steps **strictly one at a time**: safe extraction-defect repairs, re-extraction rescans of
sources whose documents were never extracted, an ANALYZE, and the roll-ups. Because it is
one job doing one thing at a time, it can never over-parallelise and swamp a small box (the
failure mode that made five concurrent rescans thrash the DB) — and it needs no operator or
LLM to drive it.

Every step is idempotent, so the whole job is naturally resumable: a repair that matches
nothing is a no-op, a rescan with ``only_unextracted`` skips finished documents, and
ANALYZE/roll-ups are recomputations. A cancel or restart simply re-runs cheaply from the
top and converges.

Runnable as the ``maintenance-run`` job (API/MCP/scheduler) or ``raglex maintain``.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger("raglex.maintenance")

# The safe, bounded, idempotent repairs run unconditionally in the auto plan (each is a
# no-op when nothing matches). Ordered gentlest first.
_SAFE_REPAIRS = (
    "resolved_dst_missing",   # non-destructive: reopen edges to a vanished target
    "duplicate_spans",        # dedupe exact-span double extractions
    "self_citation",          # drop non-structured self-edges (noise)
    "misdated_case",          # correct dates contradicted by the neutral-cite slug
    "anachronistic_eu_citation",
    "case_paragraph_carry_forward",
    "judgment_paragraph_carry_forward",
)


def _never_extracted_sources(facade) -> list[tuple[str, int]]:
    """(source, count) for judgments/decisions with text but no extraction ever run —
    the rescan targets. Same invariant as the ``never_extracted`` probe."""
    with facade._open() as (cat, _rs, _ts):
        rows = cat.conn.execute(
            """
            SELECT d.source AS source, COUNT(*) AS n FROM documents d
            WHERE d.has_text = 1 AND d.doc_type IN ('judgment', 'decision')
              AND d.last_extracted_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM citations c WHERE c.src_id = d.stable_id)
            GROUP BY d.source ORDER BY n ASC
            """).fetchall()
    return [(r["source"], r["n"]) for r in rows]


def build_plan(facade, params: dict) -> list[str]:
    """The ordered step queue. Either an explicit ``steps`` list, or the auto plan:
    safe repairs → rescan never-extracted sources (smallest first, so quick wins land before
    the multi-hour ones) → ANALYZE → citation-count roll-up → PageRank."""
    explicit = params.get("steps")
    if explicit:
        return [str(s) for s in explicit]
    plan: list[str] = []
    if not params.get("no_repairs"):
        from .ops.probes import REPAIRS
        plan += [f"repair:{n}" for n in _SAFE_REPAIRS if n in REPAIRS]
    if not params.get("no_rescans"):
        want = set(params["sources"]) if params.get("sources") else None
        for source, _n in _never_extracted_sources(facade):
            if want is None or source in want:
                plan.append(f"rescan:{source}")
    # Cheap, idempotent, and it changes what the corpus SHOWS (a pending notice
    # fronting a decided case), so it runs before the roll-ups read the corpus.
    plan.append("retire-notices")
    if not params.get("no_rollups"):
        plan += ["analyze", "counts", "authority"]
    return plan


def _run_step(facade, step: str, params: dict, on_progress: Callable, cancel_check: Callable) -> dict:
    kind, _, arg = step.partition(":")

    def inner(**p):  # forward a step's own progress, tagging the step (no 'stage' collision)
        p.setdefault("stage", f"maintenance:{step}")
        p["step"] = step
        on_progress(**p)

    if kind == "repair":
        return facade.repair_probe(arg)
    if kind == "rescan":
        return facade.rescan(source=arg, only_unextracted=True, parallel=False,
                             limit=params.get("rescan_limit"),
                             on_progress=inner, cancel_check=cancel_check)
    if kind == "analyze":
        return facade.db_maintenance(analyze=True, vacuum=bool(params.get("vacuum")))
    if kind == "vacuum":
        return facade.db_maintenance(analyze=True, vacuum=True)
    if kind == "retire-notices":
        return facade.retire_resolved_pending_notices()
    if kind == "counts":
        return facade.rebuild_citation_counts()
    if kind == "authority":
        return facade.rebuild_authority(on_progress=inner, cancel_check=cancel_check)
    return {"error": f"unknown maintenance step {step!r}"}


def run_maintenance(facade, params: dict, on_progress: Callable, cancel_check: Callable) -> dict:
    """Execute the maintenance queue serially. One step at a time; cancellable between and
    within steps; idempotent throughout."""
    steps = build_plan(facade, params or {})
    results: list[dict] = []
    total = len(steps)
    log.info("maintenance plan (%d steps): %s", total, steps)
    for i, step in enumerate(steps):
        if cancel_check and cancel_check():
            return {"cancelled": True, "completed": i, "total": total, "results": results}
        on_progress(stage="maintenance", step=step, done=i, total=total,
                    _checkpoint={"steps": steps, "done": i})
        try:
            r = _run_step(facade, step, params or {}, on_progress, cancel_check)
        except Exception as exc:  # noqa: BLE001 — one bad step must not abort the whole pass
            log.exception("maintenance step %s failed", step)
            r = {"error": str(exc)}
        results.append({"step": step, "result": r})
    on_progress(stage="maintenance", step="done", done=total, total=total)
    facade._invalidate_caches()
    return {"total": total, "results": results}
