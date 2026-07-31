# Background job progress contract

Every operation that may take more than a request/response round trip must be a
durable job in `raglex.jobs.RUNNERS`. REST, MCP, the scheduler, and the UI must
start that same named job rather than implement separate long-running paths.

Every runner receives `on_progress` and `cancel_check`. Follow this contract:

1. Emit a stage before each potentially blocking phase: discovery, each network
   fetch, parsing, storing, extraction, resolution, tagging, and finalisation.
2. Set `item` to the identifier currently being attempted. Emit this before the
   blocking call, not only after it succeeds.
3. Count work examined in `done`, not only records changed. An all-deduplicated
   or all-skipped pass is still making measurable progress.
4. Supply `total` only when it is authoritative. Unknown totals render as an
   indeterminate progress bar; a fabricated total produces a misleading ETA.
5. Keep counters scoped to the current `stage`. The job manager derives rate and
   ETA from the stage transition, so do not carry an earlier phase's counter into
   a later phase.
6. Poll `cancel_check` between items and before starting another expensive phase.
7. For resumable work, include `_checkpoint` only after the represented work is
   durable. A document reparse that also requires citation re-extraction must not
   advance its checkpoint between those two writes.
8. A discovery iterator should stop once its newest-first cursor is reached. Do
   not keep paging older records while yielding nothing.
9. Batched discovery should expose a stable cursor (`resume_offset`, page token,
   or stable identifier). Put real feed totals and positions in `Stub.hints`
   where the source supplies them.
10. Never infer that a job is dead solely because item progress is quiet. The job
    manager maintains a separate process lease. The UI reports a live but quiet
    phase as `working · quiet phase`; only an expired lease is `worker stopped`.

The job manager supplies a universal `starting {kind}` event and preserves the
runner's last real progress event at completion. That is a safety net, not a
replacement for phase/item events inside a runner. The shared ingestion pipeline already emits discovery, fetching, and
storing phases; adapters should not duplicate those, but must avoid buffering or
cursor logic that prevents the pipeline from seeing an item for long periods.

Tests for a new job should cover:

- progress when every item is skipped or unchanged;
- phase change before a deliberately blocked fetch;
- truthful total/unknown-total behaviour;
- cancellation;
- checkpoint restoration after interruption; and
- liveness classification (live quiet worker versus expired process lease).
