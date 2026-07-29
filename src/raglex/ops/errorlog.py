"""System errors as review-queue items (§8).

RagLex already has two queues a human — or an agent over MCP — works through: user
feedback (Bugs / Feature requests) and refinement flags (a passage whose citations link
to the wrong thing). What the *system* noticed about itself went nowhere: a warning in a
container log nobody reads, and a job that finished with an error nobody opened. Both of
the bugs that cost the most this month were sitting in plain text in a log for a day —
an extraction worker dying on every document, and a harvest spending 69% of its budget on
fetches that could never be built.

So errors land in the SAME place, in the same shape, and are triaged the same way:
``kind='error'`` rows in ``feedback``, listed by ``feedback(kind='error')``, closed by
``resolve_feedback``. An agent instructed to work the review queue now sees the system's
own complaints beside the users'.

Two properties make that queue readable rather than a log dump:

* **fingerprints, not occurrences.** A systemic failure repeats — thousands of times in
  one run. The fingerprint is the error's *shape*: logger + message with ids, numbers,
  urls and quoted values masked out. The first occurrence opens a row; the rest count.
* **a floor on severity, and a ceiling on volume.** Only WARNING and above, only from
  RagLex's own loggers, and at most ``RAGLEX_ERRORLOG_MAX_PER_TICK`` new fingerprints a
  minute (a storm opens a few rows, not a few thousand).

Nothing here can break the thing it is watching: every path is wrapped, and a failure to
record an error is swallowed (logging inside a logging handler is how you get recursion).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

# ids, quoted strings, numbers, urls, hashes — the parts that vary between occurrences of
# ONE problem. Masked so "HTTP 404 for …/ukut/aac/2013/0236" and "…/ewhc/2016/92" share a
# fingerprint and count as one issue rather than opening two.
_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://\S+"), "<url>"),
    # a structured identifier is the varying part too, and its court/registry token is
    # alphabetic — "ECLI:NL:HR:1996:AD250" and "ECLI:NL:GHDH:2013:3388" are one problem
    # (rechtspraak has no such judgment), not six.
    (re.compile(r"\b[A-Z]{2,6}:[A-Za-z0-9.:_-]{3,}"), "<id>"),
    (re.compile(r"'[^']{1,120}'"), "<v>"),
    (re.compile(r'"[^"]{1,120}"'), "<v>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hash>"),
    # any token CONTAINING a digit is the varying part — "ukut/aac/2013/0236",
    # "8003200_2025", "C-604/22", "404". A bare word never is.
    (re.compile(r"(?<![\w./-])[\w./-]*\d[\w./-]*"), "<n>"),
)


def fingerprint(logger_name: str, message: str) -> str:
    """The shape of an error, stable across its occurrences."""
    masked = message or ""
    for rx, repl in _MASKS:
        masked = rx.sub(repl, masked)
    masked = re.sub(r"\s+", " ", masked).strip()[:300]
    return f"{logger_name}: {masked}"


class IssueHandler(logging.Handler):
    """A logging handler that files WARNING+ records into the review queue."""

    def __init__(self, facade, *, level: int = logging.WARNING) -> None:
        super().__init__(level=level)
        self._facade = facade
        self._lock = threading.Lock()
        self._window = 0.0
        self._in_window = 0
        try:
            self._max_per_tick = int(os.environ.get("RAGLEX_ERRORLOG_MAX_PER_TICK") or 20)
        except (TypeError, ValueError):
            self._max_per_tick = 20

    def _budget_ok(self) -> bool:
        """At most N records a minute reach the DB — a storm must not become a write
        storm on the very database that is probably what is failing."""
        now = time.time()
        with self._lock:
            if now - self._window >= 60:
                self._window, self._in_window = now, 0
            if self._in_window >= self._max_per_tick:
                return False
            self._in_window += 1
            return True

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        if getattr(record, "raglex_no_issue", False) or not self._budget_ok():
            return
        try:
            message = record.getMessage()
            meta = {"logger": record.name, "level": record.levelname,
                    "module": f"{record.module}:{record.lineno}"}
            if record.exc_info:
                meta["exception"] = logging.Formatter().formatException(record.exc_info)[-2000:]
            self._facade.report_issue(
                message=message[:2000], page=record.name,
                fingerprint=fingerprint(record.name, message), metadata=meta)
        except Exception:  # noqa: BLE001 — a handler must never raise into its caller
            pass


def install(facade, *, logger_name: str = "raglex", level: int = logging.WARNING) -> IssueHandler | None:
    """Attach the handler to RagLex's logger tree (idempotent). Off with
    ``RAGLEX_ERRORLOG=0`` — the errors still go to the container log either way."""
    if not int(os.environ.get("RAGLEX_ERRORLOG", "1") or 0):
        return None
    root = logging.getLogger(logger_name)
    for h in list(root.handlers):
        if isinstance(h, IssueHandler):
            if h._facade is facade:
                return h
            # a NEW facade (a second app in-process) must not keep filing its errors into
            # the old one's database — re-point rather than stack a second handler
            root.removeHandler(h)
    handler = IssueHandler(facade, level=level)
    root.addHandler(handler)
    return handler


def report_job_failure(facade, *, kind: str, error: str,
                       job_id: str | None = None) -> None:
    """A job that ended in an error — the other half of the same queue. Fingerprinted on
    (job kind + error shape), so a job failing the same way nightly is one row with a
    count, not thirty."""
    try:
        facade.report_issue(
            message=f"job {kind} failed: {error}"[:2000],
            page=f"job:{kind}",
            fingerprint=fingerprint(f"job:{kind}", error or "unknown error"),
            metadata={"job_kind": kind, "job_id": job_id, "error": (error or "")[:2000]})
    except Exception:  # noqa: BLE001
        pass


def summarise(rows: list[dict]) -> dict:
    """Counts by kind for a queue listing — what a triage pass reads first."""
    out: dict[str, int] = {}
    for r in rows:
        out[r.get("kind") or "?"] = out.get(r.get("kind") or "?", 0) + int(r.get("seen_count") or 1)
    return out
