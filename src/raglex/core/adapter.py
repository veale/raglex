"""The adapter contract (Appendix A).

A new jurisdiction is one new adapter (§1.5). Everything downstream — dedup,
storage, catalogue, graph, embedding queue — is shared and jurisdiction-agnostic.
The orchestrator reads ``requires_js`` / ``requires_proxy`` to schedule heavy
adapters safely (§5a): REST/SPARQL adapters run many-in-parallel; headless
adapters are serialised so they don't swamp a single-operator machine.
"""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from .models import Record, Stub


@runtime_checkable
class Adapter(Protocol):
    source: str
    # floor seconds between requests — the fastest rate that avoids 429s (§1.8)
    min_interval: float
    # resource declaration — lets the orchestrator schedule heavy adapters safely
    requires_js: bool
    requires_proxy: bool

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        """Yield lightweight stubs for documents new since ``since`` (a watermark
        cursor). ``max_pages`` bounds the backfill path (§5)."""
        ...

    def fetch(self, stub: Stub) -> Record | None:
        """Fetch one document and normalise it to a ``Record``. May return None
        to drop a stub (e.g. PDF-only doc the feed can't serve as text)."""
        ...


class BaseAdapter:
    """Convenience base supplying the common defaults. Adapters may subclass this
    or simply satisfy the ``Adapter`` Protocol structurally."""

    source: str = "base"
    min_interval: float = 1.0
    requires_js: bool = False
    requires_proxy: bool = False

    def discover(self, since: str | None, *, max_pages: int | None = None) -> Iterator[Stub]:
        raise NotImplementedError

    def fetch(self, stub: Stub) -> Record | None:
        raise NotImplementedError
