"""System errors as review-queue items (§8) — one queue for what users report and what
RagLex notices about itself.

The bug this file exists to prevent: a systemic failure that repeats thousands of times
either buries the queue in copies of itself, or (worse) goes nowhere at all and sits in a
container log for a day. Both happened this month.
"""

from __future__ import annotations

import logging
import tempfile

import pytest

from raglex.config import Config
from raglex.facade import Facade
from raglex.ops.errorlog import IssueHandler, fingerprint, install, report_job_failure


def _facade() -> Facade:
    import os

    os.environ["RAGLEX_DATA_DIR"] = tempfile.mkdtemp()
    return Facade(Config.from_env())


# -- the fingerprint: the SHAPE of an error, not its occurrence ---------------

@pytest.mark.parametrize("a,b", [
    ("uk-caselaw: HTTP 404 for https://x/ukut/aac/2013/0236/data.xml",
     "uk-caselaw: HTTP 404 for https://x/ewhc/2016/92/data.xml"),
    ("[cite-extract] worker died on uket/2026/8003200_2025 — in-process",
     "[cite-extract] worker died on us/f/43/208 — in-process"),
    ("no targeted adapter for 'de:case:BGH:VZR187/02' (form: German federal decision)",
     "no targeted adapter for 'de/gesetz/bdsg1' (form: German federal decision)"),
    # the court token inside an identifier is alphabetic, so digit-masking alone left
    # one dead source looking like six separate problems
    ("fetch failed for ECLI:NL:HR:1996:AD250: nl-rechtspraak: HTTP 404 for https://x/a",
     "fetch failed for ECLI:NL:GHDH:2013:3388: nl-rechtspraak: HTTP 404 for https://x/b"),
])
def test_occurrences_of_one_problem_share_a_fingerprint(a, b):
    assert fingerprint("raglex.x", a) == fingerprint("raglex.x", b)


def test_different_problems_do_not_collide():
    assert fingerprint("raglex.x", "HTTP 404 for https://x/a") != \
        fingerprint("raglex.x", "connection pool exhausted")
    # the logger is part of the identity: the same words from two subsystems are two issues
    assert fingerprint("raglex.a", "it broke") != fingerprint("raglex.b", "it broke")


# -- the queue itself ---------------------------------------------------------

def test_repeats_are_counted_not_re_inserted():
    f = _facade()
    for _ in range(2000):
        f.report_issue(message="uk-caselaw: HTTP 404 for https://x/ukut/2016/0249/data.xml",
                       fingerprint=fingerprint("raglex.pipeline", "HTTP 404 for <url>"))
    rows = f.list_feedback(kind="error")
    assert len(rows) == 1
    assert rows[0]["seen_count"] == 2000 and rows[0]["last_seen_at"]


def test_resolving_lets_the_next_occurrence_open_a_fresh_row():
    """Which is what makes "did the fix hold?" answerable: a resolved issue that comes
    back is a NEW row, not a silently incremented count on a closed one."""
    f = _facade()
    r = f.report_issue(message="boom", fingerprint="raglex.x: boom")
    f.resolve_feedback(feedback_id=r["feedback_id"])
    f.report_issue(message="boom", fingerprint="raglex.x: boom")
    assert len(f.list_feedback(status="open", kind="error")) == 1
    assert len(f.list_feedback(status=None, kind="error")) == 2


def test_errors_and_user_feedback_share_one_queue():
    f = _facade()
    f.submit_feedback(kind="bug", message="the histogram overflows")
    f.report_issue(message="job embed failed: pool exhausted",
                   fingerprint="job:embed: pool exhausted")
    everything = f.list_feedback()
    assert {r["kind"] for r in everything} == {"bug", "error"}
    assert [r["kind"] for r in f.list_feedback(kind="error")] == ["error"]


# -- capture: a warning RagLex logs about itself becomes an item --------------

def test_logged_warnings_become_issues():
    f = _facade()
    handler = install(f)
    try:
        log = logging.getLogger("raglex.citations.stage")
        log.warning("[cite-extract] worker died on uket/2026/8003200_2025 — in-process")
        log.warning("[cite-extract] worker died on us/f/43/208 — in-process")
        rows = f.list_feedback(kind="error")
        assert len(rows) == 1 and rows[0]["seen_count"] == 2
        assert rows[0]["metadata"]["logger"] == "raglex.citations.stage"
    finally:
        logging.getLogger("raglex").removeHandler(handler)


def test_a_storm_cannot_become_a_write_storm():
    """The handler is bounded per minute: whatever is failing, the database it is failing
    into must not be asked to take a row for every line."""
    f = _facade()
    handler = IssueHandler(f)
    handler._max_per_tick = 5
    def _word(n: int) -> str:      # a DISTINCT, digit-free message per record, so the
        out = ""                   # fingerprint can't be what limits the row count
        while True:
            out, n = chr(ord("a") + n % 26) + out, n // 26
            if not n:
                return out
    for i in range(500):
        handler.emit(logging.LogRecord("raglex.x", logging.WARNING, __file__, 1,
                                       "distinct failure %s", (_word(i),), None))
    assert len(f.list_feedback(kind="error")) == 5


def test_a_failing_report_never_escapes_the_handler():
    """A handler that raises takes down the code it was watching."""
    class _Broken:
        def report_issue(self, **_kw):
            raise RuntimeError("the database is the thing that is down")

    handler = IssueHandler(_Broken())
    handler.emit(logging.LogRecord("raglex.x", logging.ERROR, __file__, 1, "boom", (), None))


def test_job_failures_land_in_the_queue():
    f = _facade()
    report_job_failure(f, kind="embed", error="connection pool exhausted", job_id="abc123")
    rows = f.list_feedback(kind="error")
    assert len(rows) == 1
    assert rows[0]["metadata"]["job_kind"] == "embed"
    assert "job embed failed" in rows[0]["message"]
