"""The in-process aggregate cache has a ceiling.

It began as a fixed set of dashboard keys — coverage, stats, corpus shape — and grew
per-QUERY families on top of it (``citing:{id}:{anchor}:{sort}:{jurisdiction}:{kind}:
{offset}:{limit}:{snippets}``, ``related:{id}:{limit}``), each entry carrying that
page's excerpts. Nothing was ever evicted: a stale entry is refreshed IN PLACE, and the
TTL governs freshness, not lifetime. Six days of serving took the API process to 9.5GB
resident with 1.4GB swapped out, on a machine already swapping.
"""
from __future__ import annotations

import pytest

from raglex.config import Config
from raglex.facade import Facade


@pytest.fixture
def facade(tmp_path) -> Facade:
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None,
    ))


def test_a_query_key_family_cannot_grow_without_bound(facade, monkeypatch):
    monkeypatch.setattr(Facade, "_CACHE_MAX_ENTRIES", 8)
    for n in range(50):
        facade._cached(f"citing:doc-{n}", 300, lambda n=n: {"n": n})
    assert len(facade._cache) == 8
    # The ceiling keeps the NEWEST, not the first eight it happened to see.
    assert {facade._cache[k][1]["n"] for k in facade._cache} == set(range(42, 50))


def test_eviction_is_by_last_read_not_last_write(facade, monkeypatch):
    """A dashboard key read on every page load must outlive an agent's one-off drill.
    Ordering by write would evict exactly the entries warm_caches() paid a ~16s cold
    scan each to build, because they are written once at startup and never again."""
    monkeypatch.setattr(Facade, "_CACHE_MAX_ENTRIES", 4)
    facade._cached("corpus-shape", 300, lambda: {"warm": True})       # written FIRST
    for n in range(3):
        facade._cached(f"citing:doc-{n}", 300, lambda n=n: {"n": n})  # cache now full

    facade._cached("corpus-shape", 300, lambda: {"never": "recomputed"})  # the homepage
    for n in range(3, 5):                    # two more drills, two evictions
        facade._cached(f"citing:doc-{n}", 300, lambda n=n: {"n": n})

    # The oldest WRITE survived because it was the most recent READ; the two drills
    # that nobody went back to are the ones that went.
    assert "corpus-shape" in facade._cache
    assert facade._cache["corpus-shape"][1] == {"warm": True}
    assert "citing:doc-0" not in facade._cache and "citing:doc-1" not in facade._cache


def test_the_ceiling_leaves_room_for_the_startup_warm_set(facade):
    """warm_caches() writes one drill slice per jurisdiction × 5 kind toggles × 2 sorts.
    A ceiling near that number would have the query traffic after a restart evicting the
    warm set faster than it was built."""
    from raglex.adapters.registry import JURISDICTION_LABELS

    warm_set = len(JURISDICTION_LABELS) * 5 * 2
    assert Facade._CACHE_MAX_ENTRIES > warm_set * 3


def test_an_evicted_key_is_recomputed_not_served_empty(facade, monkeypatch):
    """Eviction must look like a cold cache, never like an answer."""
    monkeypatch.setattr(Facade, "_CACHE_MAX_ENTRIES", 2)
    calls = []

    def _compute() -> dict:
        calls.append(1)
        return {"rows": [1, 2, 3]}

    assert facade._cached("citing:a", 300, _compute)["rows"] == [1, 2, 3]
    for n in range(5):
        facade._cached(f"citing:filler-{n}", 300, lambda: {})
    assert "citing:a" not in facade._cache
    assert facade._cached("citing:a", 300, _compute)["rows"] == [1, 2, 3]
    assert len(calls) == 2
