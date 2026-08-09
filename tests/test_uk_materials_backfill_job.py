from __future__ import annotations

from raglex import uk_materials_backfill as backfill


class _Facade:
    def __init__(self):
        self.calls: list[dict] = []
        self.note_attempt = 0

    def harvest(self, source, **kwargs):
        self.calls.append(kwargs)
        options = kwargs["options"]
        if options.get("notes"):
            self.note_attempt += 1
            # Publisher pushback must retry the identical parent batch, not skip it.
            if self.note_attempt == 1:
                return {"rate_limited": True}
            return {"discovered": len(options["ids"].split(",")), "stored": 1}
        return {"discovered": 4, "stored": 2, "rate_limited": False}

    def finish_bulk_postprocess(self, **kwargs):
        self.postprocess = kwargs
        return {"resolved_edges": 7, "tagged": 3}


def test_rate_limit_retries_same_notes_batch_then_runs_impacts_and_postprocess(monkeypatch):
    facade = _Facade()
    monkeypatch.setattr(backfill, "_candidate_ids", lambda *a, **k: ["a", "b", "c"])
    progress: list[dict] = []

    result = backfill.run_uk_materials_backfill(
        facade, {"batch_size": 2, "cooldown_seconds": 0}, lambda **p: progress.append(p),
        lambda: False,
    )

    note_ids = [c["options"]["ids"] for c in facade.calls if c["options"].get("notes")]
    assert note_ids == ["a,b", "a,b", "c"]
    assert result == {
        "discovered": 7, "fetched": 0, "stored": 4, "deduped": 0,
        "errors": 0, "resolved": 7, "tagged": 3, "candidates": 3,
        "completed": True,
    }
    checkpoints = [p.get("_checkpoint") for p in progress if p.get("_checkpoint")]
    assert {"phase": "notes", "after_stable_id": "b"} in checkpoints
    assert {"phase": "notes", "after_stable_id": "c"} in checkpoints
    assert {"phase": "impacts"} in checkpoints


def test_resume_after_stable_id_skips_completed_prefix(monkeypatch):
    facade = _Facade()
    facade.note_attempt = 1  # no synthetic rate limit on this run
    monkeypatch.setattr(backfill, "_candidate_ids", lambda *a, **k: ["a", "b", "c"])

    backfill.run_uk_materials_backfill(
        facade,
        {"after_stable_id": "b", "batch_size": 10, "cooldown_seconds": 0},
        lambda **p: None, lambda: False,
    )

    note_ids = [c["options"]["ids"] for c in facade.calls if c["options"].get("notes")]
    assert note_ids == ["c"]
