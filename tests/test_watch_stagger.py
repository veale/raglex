"""Recurring-watch staggering — equal-cadence watches must not all come due in the
same tick. Pure, network-free."""

from __future__ import annotations

import datetime as dt

from raglex.facade import _watch_phase_seconds, watch_is_due

UTC = dt.timezone.utc


def _iso(t: dt.datetime) -> str:
    return t.isoformat()


def test_never_run_is_due_immediately():
    now = dt.datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    assert watch_is_due(1, 1440, None, now) is True


def test_phase_offsets_spread_within_window():
    # 20 daily watches → their phases should occupy many distinct slots, not cluster
    phases = {_watch_phase_seconds(wid, 1440) for wid in range(1, 21)}
    assert len(phases) >= 15
    assert all(0 <= p < 1440 * 60 for p in phases)


def test_equal_cadence_watches_do_not_all_fire_same_tick():
    """After a shared first run, weekly watches come due on different days."""
    weekly = 7 * 24 * 60
    first = dt.datetime(2026, 7, 1, 0, 0, tzinfo=UTC)  # all ran together once
    # over the following 8 days, which watches are due at each midnight?
    due_days: dict[int, list[int]] = {}
    for day in range(1, 9):
        now = first + dt.timedelta(days=day)
        for wid in range(1, 9):
            if watch_is_due(wid, weekly, _iso(first), now):
                due_days.setdefault(day, []).append(wid)
    # not every watch becomes due on the same single day — the fires are spread out
    days_used = [d for d, ids in due_days.items() if ids]
    assert len(days_used) >= 3, f"weekly watches clustered onto too few days: {due_days}"


def test_fires_once_per_window_then_waits():
    cadence = 60  # hourly
    phase_s = _watch_phase_seconds(5, cadence)
    base = dt.datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
    # anchor 'now' just after this watch's slot boundary
    boundary = dt.datetime.fromtimestamp(
        (int(base.timestamp() // (cadence * 60)) + 1) * (cadence * 60) + phase_s, UTC)
    last = boundary - dt.timedelta(minutes=90)  # ran in the previous window
    assert watch_is_due(5, cadence, _iso(last), boundary + dt.timedelta(seconds=1)) is True
    # right after firing (last == boundary), it must wait, not re-fire next tick
    assert watch_is_due(5, cadence, _iso(boundary), boundary + dt.timedelta(minutes=5)) is False


def test_naive_last_run_timestamp_tolerated():
    now = dt.datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    # a stored timestamp without tzinfo must not raise
    assert isinstance(watch_is_due(3, 1440, "2026-07-22T12:00:00", now), bool)
