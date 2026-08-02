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


def test_pool_workers_actually_do_the_work(tmp_path, caplog):
    """The pool's fallback is silent and CORRECT — a worker that dies mid-document is
    re-run in the parent — so a protocol mismatch between the parent's send and the
    worker's unpack passed every parity test while the pool did nothing: no
    parallelism, a spawn burnt per document, and the runaway-regex budget (which lives
    in the worker) no longer covering the pass. The log line is the only symptom, so
    the test asserts on it."""
    import logging

    facade = Facade(_config(tmp_path))
    ids = _seed_corpus(facade, 40)
    with facade._open() as (cat, _rs, ts):
        for sid in ids:
            cat.conn.execute(
                "UPDATE documents SET last_extracted_at = NULL WHERE stable_id = ?", (sid,))
        cat.commit()
        with caplog.at_level(logging.WARNING, logger="raglex.citations.stage"):
            stats = extract_documents_parallel(cat, ts, ids, workers=2)
    assert stats.processed == 40
    assert "worker died" not in caplog.text


def test_pool_carries_the_home_instrument_to_the_worker(tmp_path):
    """A bare "Article 17" inside an instrument resolves to that instrument — the
    home_id the worker needs travels with the document, as it does on the serial path."""
    from raglex.citations.stage import _home_of

    facade = Facade(_config(tmp_path))
    with facade._open() as (cat, _rs, _ts):
        doc = cat.get_document(_seed_corpus(facade, 1)[0])
    assert len(_home_of(doc)) == 2      # the shape the worker unpacks


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


# A case short name, NOT a statutory initialism: "GDPR" and friends sit in
# _PROTECTED_SHORTHAND_TARGETS and are skipped on read, so a corpus built on them
# compares two empty sets and proves nothing about the two-phase path.
_SHORTHAND_ROW = {"shorthand": "Suncor", "candidate_id": "ewca/civ/2004/1000",
                  "entity_kind": "case", "is_abbrev": False}


def _seed_shorthand_store(cat) -> None:
    """Put the short name in the store as several documents' agreement — a pair below
    SHORTHAND_MIN_DOCS is document-local and never travels, so seeding it from one
    document would leave the corpus-wide pass with nothing to apply."""
    from raglex.citations.extractor import SHORTHAND_MIN_DOCS

    for i in range(SHORTHAND_MIN_DOCS):
        cat.add_learned_shorthands([dict(_SHORTHAND_ROW)], doc_id=f"seed/{i}")


def _seed_shorthand_corpus(facade: Facade, n: int) -> list[str]:
    """``n`` documents citing one case in full AND using a stored short name for it.

    The shorthand only links if the corpus-wide store is consulted, and the store is
    gated on the document already citing that candidate — hence both in every document.
    A case short name links only with a pincite, which is why the uses carry one.
    """
    ids = []
    for i in range(n):
        r = facade.import_bytes(
            data=(f"<p>Judgment {i}. Applying [2004] EWCA Civ 1000, the position is "
                  f"settled. Suncor, at para 30, is directly in point, and Suncor at "
                  f"paras 41-42 confirms it.</p>").encode(),
            filename=f"s{i}.html", doc_type="judgment", title=f"S{i} v T")
        ids.append(r["stable_id"])
    return ids


def test_pool_applies_stored_shorthands_exactly_as_the_serial_stage(tmp_path):
    """The two-phase pool protocol must not change what gets linked.

    The corpus-wide shorthand scan is the most expensive step in the pass, so the pool
    hands it back to the worker that still holds the text (phase two) instead of paying
    for it on the parent's single core. That is a pure performance move: the edges must
    be identical to the serial stage's, shorthand uses included.
    """
    from raglex.citations.stage import extract_document, reset_shorthand_cache

    def _run(pooled: bool) -> dict[str, set]:
        facade = Facade(_config(tmp_path / ("pool" if pooled else "serial")))
        ids = _seed_shorthand_corpus(facade, 40)
        with facade._open() as (cat, _rs, ts):
            _seed_shorthand_store(cat)
            reset_shorthand_cache()     # the store changed under a long-lived process
            for sid in ids:
                cat.conn.execute(
                    "UPDATE documents SET last_extracted_at = NULL "
                    "WHERE stable_id = ?", (sid,))
            cat.commit()
            if pooled:
                stats = extract_documents_parallel(cat, ts, ids, workers=2)
                assert stats.processed == 40 and not stats.cancelled
            else:
                for sid in ids:
                    extract_document(cat, ts, sid)
                cat.commit()
            return {sid: {(r["candidate_id"], r["dst_anchor"])
                          for r in cat.relations_for(sid)} for sid in ids}

    serial, pooled = _run(False), _run(True)
    assert set(serial) and len(serial) == len(pooled) == 40
    # same edges per document, and the shorthand pass really did fire
    for (s_sid, s_edges), (p_sid, p_edges) in zip(sorted(serial.items()),
                                                  sorted(pooled.items())):
        assert s_edges == p_edges, f"{s_sid} vs {p_sid}"
    # and it is not two empty sets agreeing: the stored short name really linked
    with Facade(_config(tmp_path / "pool"))._open() as (cat, _rs, _ts):
        assert sum(1 for sid in pooled
                   for r in cat.citations_for(sid)
                   if r["method"] == "shorthand_global") > 0


def test_shorthand_scan_falls_back_to_the_parent_when_the_worker_is_gone(tmp_path,
                                                                            monkeypatch):
    """If the hand-back to the worker fails, the parent does the scan itself.

    Phase two is an optimisation, never a correctness dependency: a torn-down worker
    must cost throughput, not edges. Here every attach send fails, so the whole run
    takes the in-parent path — and must still produce exactly the serial edges.
    """
    import raglex.citations.stage as stage

    def _edges(pooled: bool, break_attach: bool) -> dict:
        facade = Facade(_config(tmp_path / f"{pooled}-{break_attach}"))
        ids = _seed_shorthand_corpus(facade, 40)
        with facade._open() as (cat, _rs, ts):
            _seed_shorthand_store(cat)
            stage.reset_shorthand_cache()
            for sid in ids:
                cat.conn.execute(
                    "UPDATE documents SET last_extracted_at = NULL "
                    "WHERE stable_id = ?", (sid,))
            cat.commit()
            if break_attach:
                real_init = stage._PoolWorker.__init__

                def _init(self):
                    real_init(self)
                    real_send = self.conn.send

                    def _send(msg):
                        if isinstance(msg, tuple) and msg and msg[0] == stage._OP_ATTACH:
                            raise OSError("worker gone")
                        return real_send(msg)

                    self.conn.send = _send      # type: ignore[method-assign]

                monkeypatch.setattr(stage._PoolWorker, "__init__", _init)
            if pooled:
                stats = extract_documents_parallel(cat, ts, ids, workers=2)
                assert stats.processed == 40 and not stats.cancelled
            else:
                for sid in ids:
                    stage.extract_document(cat, ts, sid)
                cat.commit()
            return {sid: {(r["candidate_id"], r["dst_anchor"])
                          for r in cat.relations_for(sid)} for sid in ids}

    serial = _edges(False, False)
    fallback = _edges(True, True)
    assert len(serial) == len(fallback) == 40
    for s, f in zip(sorted(serial.values(), key=str), sorted(fallback.values(), key=str)):
        assert s == f
