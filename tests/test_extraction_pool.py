"""The parallel bulk-extraction path (§5) — parity with the serial stage, the
runaway-document budget under the pool, cancellation, and batched commits.

Uses real spawn'd workers (small pools) so the pickling/IPC path is exercised; the
corpora are tiny and synthetic.
"""

from __future__ import annotations

import pytest

from raglex.citations.stage import (
    _ExtractionGuard,
    _pool_size,
    extract_documents_parallel,
)
from raglex.config import Config
from raglex.facade import Facade


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite",
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json", embed_provider="local-hashing",
        embed_model=None,
    )


def _seed_corpus(facade: Facade, n: int) -> list[str]:
    """``n`` documents whose citations are known by construction: each cites one
    EWCA case (unique per doc) and the GDPR."""
    ids = []
    for i in range(n):
        r = facade.import_bytes(
            data=(f"<p>Judgment {i}. See [2004] EWCA Civ {1000 + i} and "
                  f"Regulation (EU) 2016/679, Article 17.</p>").encode(),
            filename=f"j{i}.html", doc_type="judgment", title=f"J{i} v K")
        ids.append(r["stable_id"])
    return ids


def test_pool_matches_the_serial_stage(tmp_path):
    """Every document extracted by the pool carries exactly the edges the serial
    stage would have written: the per-doc EWCA candidate and the GDPR, stamped."""
    facade = Facade(_config(tmp_path))
    ids = _seed_corpus(facade, 40)          # ≥ the serial threshold, so the pool engages
    with facade._open() as (cat, _rs, ts):
        # imports may have extracted inline — clear the stamps so the pool re-runs
        for sid in ids:
            cat.conn.execute(
                "UPDATE documents SET last_extracted_at = NULL WHERE stable_id = ?", (sid,))
        cat.commit()
        stats = extract_documents_parallel(cat, ts, ids, workers=2)
        assert stats.processed == 40 and not stats.cancelled
        assert stats.documents == 40        # every doc yielded citations
        for i, sid in enumerate(ids):
            cands = {r["candidate_id"] for r in cat.relations_for(sid)}
            assert f"ewca/civ/2004/{1000 + i}" in cands
            assert "32016R0679" in cands
            doc = cat.get_document(sid)
            assert doc["last_extracted_at"]  # stamped (commit batching flushed)


def test_small_batches_stay_serial(tmp_path, monkeypatch):
    """Below the threshold no pool is spawned — a watch tick must not pay 7 spawns."""
    import raglex.citations.stage as stage

    facade = Facade(_config(tmp_path))
    ids = _seed_corpus(facade, 3)
    spawned = []
    monkeypatch.setattr(stage, "_PoolWorker",
                        lambda: (_ for _ in ()).throw(AssertionError("pool spawned")))
    with facade._open() as (cat, _rs, ts):
        stats = extract_documents_parallel(cat, ts, ids, workers=4)
    assert stats.processed == 3
    assert spawned == []


def test_runaway_document_costs_one_worker_not_the_run(tmp_path, monkeypatch):
    """With a zero budget every document 'runs away': each is stamped and skipped,
    workers are killed and respawned, and the run still completes cleanly — the
    guard's semantics, per pool worker."""
    facade = Facade(_config(tmp_path))
    ids = _seed_corpus(facade, 34)
    monkeypatch.setattr(_ExtractionGuard, "timeout_s", staticmethod(lambda: 0.0001))
    with facade._open() as (cat, _rs, ts):
        for sid in ids:
            cat.conn.execute(
                "UPDATE documents SET last_extracted_at = NULL WHERE stable_id = ?", (sid,))
        cat.commit()
        stats = extract_documents_parallel(cat, ts, ids, workers=2)
        assert stats.processed == 34
        assert stats.documents == 0         # nothing yielded citations — all skipped
        for sid in ids:
            assert cat.get_document(sid)["last_extracted_at"]   # stamped → converges


def test_cancellation_stops_between_documents(tmp_path):
    facade = Facade(_config(tmp_path))
    ids = _seed_corpus(facade, 40)
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 3               # cancel early in the run

    with facade._open() as (cat, _rs, ts):
        for sid in ids:
            cat.conn.execute(
                "UPDATE documents SET last_extracted_at = NULL WHERE stable_id = ?", (sid,))
        cat.commit()
        stats = extract_documents_parallel(cat, ts, ids, workers=2, cancel_check=cancel)
    assert stats.cancelled
    assert 0 < stats.processed < 40         # in-flight finished, queue abandoned


def test_checkpoints_only_ride_commits(tmp_path):
    """Progress events between batch commits carry no checkpoint — a resume point
    must never point past rows that were not yet durable."""
    facade = Facade(_config(tmp_path))
    ids = _seed_corpus(facade, 40)
    events = []
    with facade._open() as (cat, _rs, ts):
        for sid in ids:
            cat.conn.execute(
                "UPDATE documents SET last_extracted_at = NULL WHERE stable_id = ?", (sid,))
        cat.commit()
        extract_documents_parallel(
            cat, ts, ids, workers=2, commit_every=10, report_every=1,
            checkpoint_fn=lambda done, sid: {"done": done},
            on_progress=lambda **kw: events.append(kw))
    with_cp = [e for e in events if "_checkpoint" in e]
    without_cp = [e for e in events if "_checkpoint" not in e]
    assert with_cp and without_cp
    # every checkpointed event lands on a commit boundary (or the final flush)
    for e in with_cp:
        assert e["done"] % 10 == 0 or e["done"] == 40


def test_pool_size_env_override(monkeypatch):
    monkeypatch.setenv("RAGLEX_EXTRACT_WORKERS", "3")
    assert _pool_size(None) == 3
    monkeypatch.delenv("RAGLEX_EXTRACT_WORKERS")
    assert _pool_size(5) == 5
    assert _pool_size(None) >= 1
