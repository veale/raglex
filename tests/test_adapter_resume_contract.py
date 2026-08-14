"""Every adapter that reports a resume cursor must accept one back.

An adapter puts ``resume_offset`` on its stubs so an interrupted backfill can restart
where it stopped. ``jobs`` honours that by reading the checkpoint and passing
``start_offset`` to the adapter's constructor on the retry. An adapter that reports the
cursor without accepting it therefore raises ``TypeError`` the moment it is resumed.

That failure is silent in the worst possible way. The retry is recorded with
``status='done'`` and an ``error`` buried in its ``result_json``, so a backfill that
stopped 14,000 documents into 357,000 reads as **finished** in every list, panel and
count. On 2026-08-14 a routine redeploy interrupted four backfills — Austria's VwGH,
LVwG and BVwG and Estonia's lahend.ee — and all four resumed, crashed on the keyword,
and filed themselves as complete. Nothing in the corpus said anything was missing.

So this is a structural test over the whole registry rather than a test of any one
adapter: it is precisely the class of bug that no author notices, because it cannot
happen until something interrupts a long run.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from raglex.adapters import registry

ADAPTERS_DIR = Path(registry.__file__).parent
#: How ``jobs`` restores a checkpointed cursor (see ``jobs._resume_params``).
RESUME_KEYWORD = "start_offset"
CURSOR_HINT = "resume_offset"


def _modules_reporting_a_cursor() -> list[Path]:
    return sorted(p for p in ADAPTERS_DIR.glob("*.py")
                  if CURSOR_HINT in p.read_text(encoding="utf-8"))


def _accepts(factory, keyword: str) -> bool:
    """Whether this factory would tolerate the keyword, lambdas and partials included."""
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):       # a C callable — assume the class below
        return False
    for parameter in signature.parameters.values():
        if parameter.name == keyword:
            return True
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            # ``lambda **kw: Adapter(..., **kw)`` — the wrapper is transparent, so the
            # question is really about what it wraps. Resolved by construction below.
            return True
    return False


def test_every_adapter_module_that_reports_a_cursor_names_the_keyword():
    """A cheap textual check that fails on the file the author is actually editing."""
    missing = [p.name for p in _modules_reporting_a_cursor()
               if RESUME_KEYWORD not in p.read_text(encoding="utf-8")]
    assert not missing, (
        f"{missing} set Stub.hints['{CURSOR_HINT}'] but never accept "
        f"'{RESUME_KEYWORD}'. jobs passes it to the constructor when it resumes an "
        f"interrupted backfill, so these adapters raise TypeError on resume and the "
        f"retry is filed as done. See core.adapter.resume_floor.")


@pytest.mark.parametrize("key", sorted(registry.ADAPTERS))
def test_a_registered_adapter_can_be_resumed(key):
    """The real contract: construct it the way ``jobs`` does on a retry.

    Only adapters that report a cursor are required to take one; the rest are skipped,
    because an adapter with no resumable cursor is never handed one.
    """
    factory = registry.ADAPTERS[key]
    try:
        adapter = factory()
    except Exception:                     # needs credentials or a path — nothing to test
        pytest.skip(f"{key} cannot be constructed with defaults")
    module = Path(inspect.getfile(type(adapter)))
    if CURSOR_HINT not in module.read_text(encoding="utf-8"):
        pytest.skip(f"{key} reports no resume cursor")
    try:
        factory(**{RESUME_KEYWORD: 100})
    except TypeError as exc:
        pytest.fail(
            f"{key} reports '{CURSOR_HINT}' but resuming it raises: {exc}. "
            f"Accept '{RESUME_KEYWORD}' and pass it through core.adapter.resume_floor.")


def test_resume_floor_lands_before_the_checkpoint_never_after():
    """Re-covering a page costs a listing request; resuming late loses a document."""
    from raglex.core.adapter import resume_floor

    assert resume_floor(13958, 100) == 13858
    assert resume_floor(50, 100) == 0
    assert resume_floor(0, 100) == 0
    assert resume_floor(None, 100) == 0
    assert resume_floor("820", 100) == 720
    # A page size of zero must not make the floor equal the checkpoint.
    assert resume_floor(500, 0) < 500


@pytest.mark.parametrize("key,offset", [
    ("at-vwgh", 13958), ("at-lvwg", 70984), ("at-bvwg", 820),
    ("ee-lahend", 50142), ("sk-ress", 1000), ("se-domstol", 5000),
    ("fi-kko", 2000), ("se-domstol-bulk", 30),
])
def test_the_adapters_whose_backfills_were_lost_now_resume(key, offset):
    """The exact four that crashed on 2026-08-14, and their siblings."""
    adapter = registry.ADAPTERS[key](**{RESUME_KEYWORD: offset})
    assert adapter is not None


def test_a_finnish_resume_cursor_counts_the_whole_feed_not_one_year():
    """Finlex is walked year by year, newest first. A per-slice counter would restart a
    1979-onwards backfill in the middle of whichever year it happened to reach."""
    source = (ADAPTERS_DIR / "fi_finlex.py").read_text(encoding="utf-8")
    walk = source[source.index("def _walk("):]
    walk = walk[:walk.index("\n    def ", 1)]
    assert re.search(r"offset=self\._emitted", walk), (
        "fi_finlex must checkpoint a feed-wide offset, not the per-year one")


# ── the cursor has to measure what was WALKED, not what was yielded ──────────
class _FakeRulings:
    """lahend.ee's JSON-RPC search, with a fixed number of rulings per month."""

    def __init__(self, per_month: int = 40):
        self.per_month = per_month
        self.calls = 0

    def __call__(self, method, arguments):
        self.calls += 1
        offset = int(arguments.get("offset") or 0)
        limit = int(arguments.get("limit") or 20)
        remaining = max(0, self.per_month - offset)
        n = min(limit, remaining)
        month = str(arguments.get("date_from"))[:7]
        return {"total": self.per_month,
                "rulings": [{"id": f"ruling:{month}-{offset + i}",
                             "caseNo": f"2-{offset + i}-1/{month[:4]}",
                             "court": "Riigikohus", "date": f"{month}-01"}
                            for i in range(n)]}


def test_a_resumed_estonian_walk_still_yields_something():
    """The bug this exists for: measuring the cursor against *yields* rather than
    against rows walked. Nothing below the checkpoint is yielded, so the counter never
    advances, so nothing is ever above the checkpoint — the resumed backfill walks the
    whole register and emits zero. It reports success, having harvested nothing.
    """
    from raglex.adapters.ee_lahend import EstonianLahendAdapter

    fake = _FakeRulings(per_month=40)
    adapter = EstonianLahendAdapter(start_date="2024-01-01", end_date="2024-12-31",
                                    start_offset=200)
    adapter._call = fake

    stubs = list(adapter.discover(None))
    assert stubs, "a resumed walk yielded nothing at all"
    # 12 months x 40 = 480 rulings; resume_floor backs 200 off by one page.
    assert len(stubs) < 480, "the cursor was ignored — everything was re-emitted"
    assert len(stubs) >= 280, f"the cursor skipped too far ({len(stubs)} of 480)"


def test_an_unresumed_estonian_walk_yields_everything():
    from raglex.adapters.ee_lahend import EstonianLahendAdapter

    adapter = EstonianLahendAdapter(start_date="2024-01-01", end_date="2024-12-31")
    adapter._call = _FakeRulings(per_month=40)
    assert len(list(adapter.discover(None))) == 480


def test_the_estonian_cursor_it_reports_is_the_one_it_accepts_back():
    """A checkpoint is only useful if the number written is on the same scale as the
    number read. Feed-wide out, feed-wide in."""
    from raglex.adapters.ee_lahend import EstonianLahendAdapter

    adapter = EstonianLahendAdapter(start_date="2024-01-01", end_date="2024-03-31")
    adapter._call = _FakeRulings(per_month=40)
    offsets = [s.hints["resume_offset"] for s in adapter.discover(None)]
    assert offsets == list(range(120)), "the cursor restarted inside each month"


def test_a_resumed_estonian_walk_steps_over_whole_months_in_one_request():
    """Reaching a cursor must not cost a request per 25 rulings.

    lahend.ee reports a window's ``total`` on that window's first call, so a month lying
    wholly below the checkpoint costs exactly one request. Paging through it instead is
    the difference between a resume that is quiet for eight minutes and one that is quiet
    for fifty — and every one of those requests re-reads documents already held.
    """
    from raglex.adapters.ee_lahend import EstonianLahendAdapter

    fake = _FakeRulings(per_month=40)          # 12 months x 40 = 480, 2 pages each
    adapter = EstonianLahendAdapter(start_date="2024-01-01", end_date="2024-12-31",
                                    start_offset=400)
    adapter._call = fake
    stubs = list(adapter.discover(None))

    assert stubs, "a resumed walk yielded nothing"
    # 9 whole months skipped at one call each, then the remaining months paged.
    assert fake.calls <= 16, f"{fake.calls} requests to reach the cursor — months not skipped"
    # …and the skipping must not overshoot: everything at or above the floor is still here.
    assert len(stubs) >= 105, f"skipped past live documents ({len(stubs)} yielded)"


def test_the_month_skip_never_runs_twice_for_one_month():
    """``total`` is the whole month. Applied on the second page as well, it would step
    over the month twice and silently skip documents the corpus does not hold."""
    from raglex.adapters.ee_lahend import EstonianLahendAdapter

    adapter = EstonianLahendAdapter(start_date="2024-01-01", end_date="2024-04-30",
                                    start_offset=50)
    adapter._call = _FakeRulings(per_month=40)   # 4 months x 40 = 160
    offsets = [s.hints["resume_offset"] for s in adapter.discover(None)]
    # resume_floor backs 50 off one page (25) → 25; everything from 25 on must survive.
    assert offsets == list(range(25, 160)), "the walk skipped or repeated a month"
