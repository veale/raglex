"""Durable corpus-wide backfill for legislation.gov.uk companion material.

The ordinary materials adapter deliberately accepts a bounded list of parent ids.  A full
corpus pass has one extra responsibility: derive that list from the UK instruments already
held locally, checkpoint between small batches, and wait (without losing the batch) when the
publisher asks us to slow down.  Keeping that orchestration here makes the operation a real
RagLex job rather than an invisible one-off container.
"""

from __future__ import annotations

import time
from bisect import bisect_right
from pathlib import Path


_NOTE_MARKERS = (b"/notes", b"/memorandum", b"/executive-note", b"/policy-note")


def _wait(seconds: float, *, on_progress, cancel_check, stage: str, done: int, total: int) -> bool:
    """Interruptible publisher cooldown.  True means the job was cancelled."""
    until = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < until:
        if cancel_check and cancel_check():
            return True
        remaining = max(0, round(until - time.monotonic()))
        on_progress(stage=stage, done=done, total=total,
                    msg=f"publisher cooldown — retrying this batch in {remaining}s")
        time.sleep(min(10.0, max(0.0, until - time.monotonic())))
    return bool(cancel_check and cancel_check())


def _candidate_ids(facade, *, on_progress, cancel_check) -> list[str]:
    with facade._open() as (cat, _rs, _ts):
        rows = cat.conn.execute(
            "SELECT stable_id, raw_path FROM documents "
            "WHERE source=? AND doc_type=? AND stable_id NOT LIKE ? ORDER BY stable_id",
            ("uk-legislation", "legislation", "%@%"),
        ).fetchall()
    found: list[str] = []
    total = len(rows)
    for index, row in enumerate(rows, 1):
        if cancel_check and cancel_check():
            break
        try:
            raw = Path(row["raw_path"]).read_bytes() if row["raw_path"] else b""
        except OSError:
            continue
        if any(marker in raw for marker in _NOTE_MARKERS):
            found.append(str(row["stable_id"]))
        if index == 1 or index % 1000 == 0 or index == total:
            on_progress(stage="finding UK instruments with explanatory material",
                        done=index, total=total, item=str(row["stable_id"]),
                        msg=f"{len(found):,} candidates")
    return found


def run_uk_materials_backfill(facade, params: dict, on_progress, cancel_check) -> dict:
    """Backfill notes, then impacts, then resolve/tag once; fully job-checkpointed."""
    phase = str(params.get("start_phase") or "notes")
    after_stable_id = str(params.get("after_stable_id") or "")
    batch_size = max(1, int(params.get("batch_size") or 100))
    cooldown = max(0.0, float(
        params["cooldown_seconds"] if params.get("cooldown_seconds") is not None else 300
    ))
    totals = {"discovered": 0, "fetched": 0, "stored": 0, "deduped": 0, "errors": 0}

    candidates = _candidate_ids(
        facade, on_progress=on_progress, cancel_check=cancel_check)
    if cancel_check and cancel_check():
        return {**totals, "cancelled": True, "candidates": len(candidates)}

    if phase == "notes":
        start = bisect_right(candidates, after_stable_id) if after_stable_id else 0
        while start < len(candidates):
            if cancel_check and cancel_check():
                return {**totals, "cancelled": True, "candidates": len(candidates)}
            chunk = candidates[start:start + batch_size]

            def note_progress(**progress) -> None:
                item = progress.get("item") or chunk[0]
                on_progress(stage="backfilling UK explanatory material",
                            done=start, total=len(candidates), item=item,
                            _checkpoint={"phase": "notes",
                                         "after_stable_id": after_stable_id})

            out = facade.harvest(
                "uk-legislation-materials", backfill=True, max_pages=None,
                options={"ids": ",".join(chunk), "notes": True, "impacts": False},
                refetch_held=False, resolve=False, on_progress=note_progress,
                cancel_check=cancel_check,
            )
            if out.get("error"):
                raise RuntimeError(str(out["error"]))
            if out.get("rate_limited"):
                if _wait(cooldown, on_progress=on_progress, cancel_check=cancel_check,
                         stage="backfilling UK explanatory material", done=start,
                         total=len(candidates)):
                    return {**totals, "cancelled": True, "candidates": len(candidates)}
                continue  # same batch: a rate limit can never advance the checkpoint
            for key in totals:
                totals[key] += int(out.get(key) or 0)
            start += len(chunk)
            after_stable_id = chunk[-1]
            on_progress(
                stage="backfilling UK explanatory material", done=start,
                total=len(candidates), item=after_stable_id,
                msg=f"{totals['stored']:,} newly stored",
                _checkpoint={"phase": "notes", "after_stable_id": after_stable_id},
            )
        phase = "impacts"

    if phase == "impacts":
        on_progress(stage="backfilling UK impact assessments", done=0,
                    _checkpoint={"phase": "impacts"})
        while True:
            if cancel_check and cancel_check():
                return {**totals, "cancelled": True, "candidates": len(candidates)}

            def impact_progress(**progress) -> None:
                on_progress(stage="backfilling UK impact assessments",
                            done=int(progress.get("done") or 0),
                            item=progress.get("item"),
                            _checkpoint={"phase": "impacts"})

            out = facade.harvest(
                "uk-legislation-materials", backfill=True, max_pages=None,
                options={"notes": False, "impacts": True}, refetch_held=False,
                resolve=False, force_full=True, on_progress=impact_progress,
                cancel_check=cancel_check,
            )
            if out.get("error"):
                raise RuntimeError(str(out["error"]))
            for key in totals:
                totals[key] += int(out.get(key) or 0)
            if not out.get("rate_limited"):
                break
            if _wait(cooldown, on_progress=on_progress, cancel_check=cancel_check,
                     stage="backfilling UK impact assessments", done=0, total=0):
                return {**totals, "cancelled": True, "candidates": len(candidates)}
        phase = "postprocess"

    if phase == "postprocess":
        after_relation_id = int(params.get("after_relation_id") or 0)
        tag_start = int(params.get("tag_start") or 0)
        resolve = not bool(params.get("resolution_complete"))

        def post_progress(**progress) -> None:
            checkpoint = progress.pop("_checkpoint", None) or {}
            subphase = checkpoint.get("phase")
            outer = {"phase": "postprocess"}
            if subphase == "resolve" and checkpoint.get("relation_id") is not None:
                outer["after_relation_id"] = int(checkpoint["relation_id"])
            elif subphase == "tag":
                outer["resolution_complete"] = True
                if checkpoint.get("completed") is not None:
                    outer["tag_start"] = int(checkpoint["completed"])
            on_progress(**progress, _checkpoint=outer)

        post = facade.finish_bulk_postprocess(
            source="uk-legislation-materials", resolve=resolve, tag=True,
            after_relation_id=after_relation_id, tag_start=tag_start,
            on_progress=post_progress, cancel_check=cancel_check,
        )
        totals["resolved"] = int(post.get("resolved_edges") or 0)
        totals["tagged"] = int(post.get("tagged") or 0)

    return {**totals, "candidates": len(candidates), "completed": True}
