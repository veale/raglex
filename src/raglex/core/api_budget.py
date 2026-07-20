"""A persistent rolling-window request budget for hard-capped external APIs (§1.8).

``RateLimitedClient`` paces requests and backs off when a source pushes back. That is
the right model for a source whose limit is "don't be rude". It is the wrong model for
one with a **hard daily quota**: CourtListener's free tier allows 125 requests a day,
and pacing cannot help you once they're spent — you simply have no more requests until
old ones age out of the window. Backing off and retrying a 429 in that state burns the
retry budget to earn another 429.

So this is a *ledger*, not a throttle. Every request is recorded with its timestamp;
before each one we ask whether every window still has room, and if not we say so
**without making the call**. Three properties matter:

* **Persistent.** The windows are hours long and jobs are minutes long, so an
  in-process counter would reset the budget on every run and overrun the quota by an
  order of magnitude. The ledger is a SQLite file beside the catalogue.
* **Shared.** The web app, the scheduler tick and a CLI run all spend the same
  account's quota. Keying by source (not by process) is what makes the total honest.
* **Rolling, not calendar.** CourtListener's windows refill gradually as old requests
  age out; there is no midnight reset to wait for. ``retry_after`` is computed from
  the oldest request in the binding window — the exact moment capacity returns.

The count is necessarily a slight *under*-estimate of the account's true usage: only
requests made through RagLex are recorded, and the quota is per-account. If the same
token is used elsewhere, expect real 429s despite a ledger with room — which the HTTP
client still handles. This is a budget, not a guarantee.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_requests (
    source     TEXT NOT NULL,
    at         REAL NOT NULL   -- unix seconds
);
CREATE INDEX IF NOT EXISTS api_requests_source_at_idx ON api_requests (source, at);
"""


@dataclass(frozen=True, slots=True)
class Window:
    """One rolling limit: ``limit`` requests per ``seconds``."""

    name: str
    seconds: float
    limit: int


@dataclass(frozen=True, slots=True)
class BudgetState:
    allowed: bool
    # the window that is currently binding, when we're out of room
    blocked_by: str | None
    # seconds until the binding window has room again (0 when allowed)
    retry_after: float
    # per-window {name: (used, limit)} — what a UI shows the operator
    windows: dict[str, tuple[int, int]]

    @property
    def remaining(self) -> int:
        """Requests available right now: the tightest window's headroom."""
        return min((limit - used for used, limit in self.windows.values()), default=0)


class BudgetExhausted(RuntimeError):
    """Raised instead of making a request that the quota cannot pay for.

    Carries ``retry_after`` so callers can report *when* work resumes rather than
    just that it stopped — the difference between a useful message and a mystery.
    """

    def __init__(self, source: str, state: BudgetState) -> None:
        super().__init__(
            f"{source}: API budget exhausted ({state.blocked_by} window); "
            f"retry in {state.retry_after:.0f}s")
        self.source = source
        self.state = state
        self.retry_after = state.retry_after


class RequestBudget:
    """A rolling-window ledger for one source's API quota.

    ``spend()`` is the whole interface: it either records a request and returns, or
    raises ``BudgetExhausted``. Record the request *before* making it — an attempt
    that fails still consumed quota upstream (a 429 counts against you), so charging
    on success would let a failing loop spend the day's budget invisibly.
    """

    def __init__(self, source: str, windows: tuple[Window, ...], *,
                 path: str | Path | None = None, now=time.time) -> None:
        self.source = source
        self.windows = windows
        self._now = now
        self.path = str(path) if path else ":memory:"
        if self.path != ":memory:":
            # The ledger can be constructed before anything else has touched the data
            # dir (an adapter built to answer "what's my quota?" on a fresh install),
            # so don't assume the directory exists — sqlite would fail to open rather
            # than create it, and the adapter would be unusable for a missing folder.
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A busy timeout, because this file is shared by design: the scheduler tick,
        # the web app and a CLI run all spend the same account's quota concurrently.
        # sqlite's default is to fail instantly on a held write lock, which would turn
        # routine contention into "database is locked" in the middle of a harvest.
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        # WAL so the scheduler tick and a web request can spend the same budget
        # concurrently without one blocking the other into a timeout.
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "RequestBudget":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- state ---------------------------------------------------------------
    def state(self, n: int = 1) -> BudgetState:
        """Headroom in every window, without spending anything.

        ``n`` is the size of the spend being contemplated (a case costs one request per
        opinion). It matters for more than the yes/no: a window with 2 slots left is
        not "full", but it still blocks a spend of 4, and the operator needs to be told
        *which* window and *when* — "budget exhausted (None window), retry in 0s" is
        the failure this parameter exists to prevent.

        Read-only. Pruning happens in ``spend`` instead: a dashboard polling the budget
        must not need the write lock, or a UI refresh can collide with a running
        harvest and fail as "database is locked".
        """
        now = self._now()
        used: dict[str, tuple[int, int]] = {}
        blocked_by: str | None = None
        retry_after = 0.0
        for w in self.windows:
            cutoff = now - w.seconds
            rows = self._conn.execute(
                "SELECT at FROM api_requests WHERE source = ? AND at >= ? ORDER BY at",
                (self.source, cutoff)).fetchall()
            count = len(rows)
            used[w.name] = (count, w.limit)
            shortfall = count + max(1, n) - w.limit
            if shortfall > 0:
                # Capacity for the whole spend returns once `shortfall` of the oldest
                # requests have aged out — not just the single oldest. Waiting on the
                # first one and retrying a spend that still doesn't fit is how a queue
                # busy-loops against its own budget.
                idx = min(shortfall, count) - 1
                oldest = rows[idx]["at"] if idx >= 0 else now
                wait = max(0.0, oldest + w.seconds - now)
                if wait >= retry_after:
                    retry_after, blocked_by = wait, w.name
        return BudgetState(allowed=blocked_by is None, blocked_by=blocked_by,
                           retry_after=retry_after, windows=used)

    def can_spend(self, n: int = 1) -> bool:
        """Would a spend of ``n`` be accepted right now?"""
        return self.state(n).allowed

    # -- spending ------------------------------------------------------------
    def spend(self, n: int = 1) -> BudgetState:
        """Charge ``n`` requests, or raise ``BudgetExhausted`` if they don't fit.

        All-or-nothing: a caller that needs 4 requests to assemble one case (a cluster
        plus three opinions) should ask for 4 up front rather than stranding itself
        half-way through with an unusable partial record.
        """
        state = self.state(n)
        if not state.allowed:
            raise BudgetExhausted(self.source, state)
        now = self._now()
        # Prune and insert in the one transaction, so the write lock is taken once per
        # spend rather than on every read of the budget.
        self._prune(now)
        self._conn.executemany(
            "INSERT INTO api_requests (source, at) VALUES (?, ?)",
            [(self.source, now)] * n)
        self._conn.commit()
        return self.state()

    def _prune(self, now: float) -> None:
        """Drop rows older than the longest window — they can never bind again.

        Without this the table grows forever for no benefit; with it the ledger stays
        at most a day's requests, so the COUNT queries stay trivial.
        """
        longest = max((w.seconds for w in self.windows), default=0.0)
        self._conn.execute("DELETE FROM api_requests WHERE source = ? AND at < ?",
                           (self.source, now - longest * 2))

    def reset(self) -> None:
        """Forget this source's recorded requests.

        For an operator whose quota was raised (a Free Law Project membership) or who
        knows the ledger over-counts after a crash — not something normal operation
        needs.
        """
        self._conn.execute("DELETE FROM api_requests WHERE source = ?", (self.source,))
        self._conn.commit()


__all__ = ["BudgetExhausted", "BudgetState", "RequestBudget", "Window"]
