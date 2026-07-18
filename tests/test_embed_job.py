"""Embedding as a resumable, progress-reporting background job — plus the backlog gauge
and the search payoff (content/freetext, not just title, becomes findable once indexed)."""

from __future__ import annotations

import pytest

from raglex.config import Config
from raglex.core.models import AddedBy, DocType, ExtractedVia, Record
from raglex.facade import Facade
from raglex.jobs import RUNNERS, SINGLETON_KINDS
from raglex.settings import KNOWN_SETTINGS


@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json", embed_provider="local-hashing", embed_model=None,
    ))


def _doc(cat, ts, sid: str, text: str):
    ph = sid.encode().hex()[:16]
    ts.put(ph, text)
    cat.upsert_document(Record(
        source="uk-caselaw", stable_id=sid, doc_type=DocType.JUDGMENT, title=sid,
        raw_bytes=text.encode(), raw_ext="txt", payload_hash=ph, text=text,
        extracted_via=ExtractedVia.SCRAPE, added_by=AddedBy.USER),
        text_path=str(ts.put(ph, text)))


def test_embed_is_a_registered_background_job():
    assert "embed" in RUNNERS
    # a singleton: two concurrent passes would race over the same pending_embedding queue
    assert "embed" in SINGLETON_KINDS
    # and the scheduler knob is a declared setting, so it's editable in the UI
    assert any(s.key == "RAGLEX_AUTOEMBED" for s in KNOWN_SETTINGS)


def test_backlog_tracks_indexing_and_embed_drains_it(facade):
    with facade._open() as (cat, _rs, ts):
        _doc(cat, ts, "a/1", "The rule against double recovery and abuse of process.")
        _doc(cat, ts, "a/2", "Estoppel per rem judicatam and the same subject matter.")
        cat.commit()

    assert facade.embedding_backlog()["pending"] == 2
    ticks = []
    stats = facade.embed(on_progress=lambda **k: ticks.append((k.get("done"), k.get("total"))))
    assert stats["documents"] == 2 and stats["chunks"] > 0
    assert ticks and ticks[-1] == (2, 2)                      # progress reported to completion
    bl = facade.embedding_backlog()
    assert bl["pending"] == 0 and bl["indexed"] == 2         # backlog drained


def test_embed_resumes_only_the_unindexed(facade):
    with facade._open() as (cat, _rs, ts):
        _doc(cat, ts, "a/1", "First judgment text about limitation.")
        cat.commit()
    facade.embed()                                            # index the first
    with facade._open() as (cat, _rs, ts):
        _doc(cat, ts, "a/2", "Second judgment text about jurisdiction.")
        cat.commit()
    # a re-run only touches the newly-added doc (the queue drains monotonically)
    stats = facade.embed()
    assert stats["documents"] == 1


def test_embed_is_cancellable_midway(facade):
    with facade._open() as (cat, _rs, ts):
        for i in range(4):
            _doc(cat, ts, f"a/{i}", f"Judgment {i} about contract and tort.")
        cat.commit()
    seen = {"n": 0}

    def cancel():
        seen["n"] += 1
        return seen["n"] > 2                                  # stop after the 2nd check
    stats = facade.embed(cancel_check=cancel)
    assert stats["documents"] < 4                            # stopped early
    assert facade.embedding_backlog()["pending"] > 0         # remainder still queued


def test_indexing_makes_body_text_searchable(facade):
    with facade._open() as (cat, _rs, ts):
        _doc(cat, ts, "a/1", "A judgment discussing the doctrine of abuse of process in detail.")
        cat.commit()
    facade.embed()
    hits = facade.search("abuse of process", k=3)
    assert any(h["doc_id"] == "a/1" for h in hits)           # found by body, not title
