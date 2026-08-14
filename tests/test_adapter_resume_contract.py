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
