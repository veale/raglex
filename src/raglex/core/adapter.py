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


def option_flag(value, default: bool) -> bool:
    """A ``SourceOption`` boolean as the user may actually have sent it.

    Every option reaches a constructor as whatever the REST/MCP caller put in the form,
    so a checkbox arrives as ``True``, ``"true"``, ``"on"``, ``"0"`` — or as ``None``
    when it was left alone. ``bool(None)`` is False, which silently turns a default-on
    option off for anyone who did not touch it; ``None`` must mean "the default"."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def resume_floor(start_offset, page_size: int) -> int:
    """Where a discovery resumed from a checkpoint should actually restart.

    One page earlier than the checkpoint said. Re-covering a page costs one listing
    request and nothing else, because the pipeline drops a stub whose document it already
    holds; resuming even one item *late* loses that document for good. Page arithmetic
    always drifts a little — a stub filtered out on the first pass advances the offset
    without advancing the page — so the error has to be pushed to the harmless side
    deliberately rather than hoped away.

    Every adapter that puts ``resume_offset`` on its stubs is promising to accept
    ``start_offset`` back: ``jobs`` reads the checkpoint and passes it to the constructor,
    and an adapter that does not take the keyword raises ``TypeError`` on resume. That
    failure is silent in the worst way — the retry is marked *done* with an error in its
    result, so an interrupted backfill looks finished. Four of them were, on 2026-08-14.
    """
    return max(0, int(start_offset or 0) - max(1, int(page_size or 1)))


def option_int(value, default: int) -> int:
    """A ``SourceOption`` integer, with ``None``/blank/unparseable meaning the default."""
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
