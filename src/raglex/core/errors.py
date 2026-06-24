"""Typed errors for the harvest pipeline.

The key distinction the orchestrator cares about is *transient vs fatal* (§A
backoff, §5a quarantine): a transient error is retried with backoff; a
``RateLimitException`` pauses only the offending source's queue rather than
crashing the run or poisoning sibling sources.
"""

from __future__ import annotations


class RaglexError(Exception):
    """Base class for all RagLex errors."""


class AdapterError(RaglexError):
    """A source adapter failed to discover or fetch."""


class FetchError(AdapterError):
    """A document fetch failed. ``transient`` decides retry vs skip (§A)."""

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


class RateLimitException(AdapterError):
    """Raised when an adapter hits a 429/503/WAF wall (§5a, Appendix A).

    The orchestrator catches this and *pauses that source's queue* — it does not
    fail the whole run. ``retry_after`` carries an upstream ``Retry-After`` hint
    (seconds) when the source provides one.
    """

    def __init__(self, source: str, *, retry_after: float | None = None) -> None:
        super().__init__(f"rate limited by source {source!r}")
        self.source = source
        self.retry_after = retry_after
