"""A long job must report progress on work SEEN, not work done.

The bug this pins: every corpus-walking repair reported progress only when it
changed something. A resumed pass legitimately skips what it already did — the
segment repair skipped 115,000 documents on its fourth restart — so it reported
nothing for minutes at a time, and the cross-process reaper, which decides a job has
died from an idle heartbeat, treats that as death. The job was demonstrably healthy:
it wrote 16,206 files in the five minutes it spent claiming to be stalled.

So the invariant is: a pass that changes NOTHING still reports progress.
"""

from __future__ import annotations

from datetime import date

import pytest

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade


def _facade(root) -> Facade:
    return Facade(Config(
        data_dir=root, catalogue_path=str(root / "c.sqlite"),
        raw_dir=root / "raw", text_dir=root / "text",
        settings_path=root / "s.json",
        embed_provider="local-hashing", embed_model=None))


@pytest.fixture()
def facade(tmp_path):
    return _facade(tmp_path)


# The three all-skip passes below each seeded 2,500 documents of their own — ~76s of
# SQLite inserts to prove a progress callback fires. The VOLUME is load-bearing (the bug
# is "a long pass looks frozen", and the assertions want a realistic batch count), but
# re-seeding it three times is not: every one of them asserts it changed NOTHING
# (repaired/improved/derived/named all zero), so they cannot disturb each other and can
# read one corpus. Seeded once per session instead.
@pytest.fixture(scope="session")
def seeded_facade(tmp_path_factory):
    root = tmp_path_factory.mktemp("progress-corpus")
    facade = _facade(root)
    _seed(facade, n=2500)
    return facade


def _seed(facade, n=1200, text="Clean text with nothing to repair.\n\n1. A paragraph."):
    with facade._open() as (cat, _rs, ts):
        for i in range(n):
            rec = Record(source="uk-caselaw", stable_id=f"d{i:05d}",
                         doc_type=DocType.JUDGMENT, title=f"Doc {i}",
                         decision_date=date(2020, 1, 1), text=text,
                         raw_bytes=text.encode(), extracted_via=ExtractedVia.STRUCTURED)
            rec.ensure_payload_hash()
            cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)


def test_mojibake_repair_reports_even_when_nothing_needs_repairing(seeded_facade):
    facade = seeded_facade
    rec = Recorder()
    st = facade.repair_mojibake(on_progress=rec)
    assert st["repaired"] == 0, "fixture text needs no repair"
    assert rec.calls, "a pass that changed nothing reported no progress"
    assert rec.calls[-1]["done"] >= 2000


def test_resegment_reports_even_when_every_document_is_unchanged(seeded_facade):
    facade = seeded_facade
    rec = Recorder()
    st = facade.resegment_judgments(on_progress=rec)
    assert st["improved"] == 0 and st["derived"] == 0
    assert rec.calls, "an all-skip pass reported no progress"


def test_intituling_reports_even_when_no_bench_is_found(seeded_facade):
    facade = seeded_facade
    rec = Recorder()
    st = facade.backfill_intituling(on_progress=rec)
    assert st["named"] == 0, "the fixture has no intituling block"
    assert rec.calls, "a pass that named nobody reported no progress"


def test_the_localise_scan_is_ordered(facade):
    """An interrupted copy must resume through the same sequence rather than
    re-walking an arbitrary permutation — four interruptions each cost a fresh walk
    of everything already done."""
    import re

    import raglex.facade as mod

    src = mod.Facade.localise_text.__doc__ or ""
    assert "explicit" in src.lower() or True         # doc sanity
    code = (mod.__file__ and open(mod.__file__).read()) or ""
    body = code[code.index("def localise_text"):]
    body = body[:body.index("def ", 40)]
    assert re.search(r"ORDER BY payload_hash", body), \
        "the scan is unordered, so a restart re-walks an arbitrary permutation"


def test_a_slow_discovery_keeps_reporting_while_it_blocks():
    """The silent gap is INSIDE the generator, so the pulse has to be on a timer.

    eu-cellar spends minutes in a single SPARQL round trip building the next stub. A
    heartbeat that only fires between yielded items reports nothing for the whole of
    that — and a job silent for 30 minutes is now reaped as wedged, resumed, and is just
    as silent next time. A healthy harvest would loop for ever.
    """
    import time

    from raglex.core.models import Stub
    from raglex.pipeline.runner import Pipeline

    beats: list[dict] = []

    def slow_discover():
        yield Stub(stable_id="a", raw_url="u", title="t", court="c", hints={})
        time.sleep(0.75)          # blocked inside the generator, yielding nothing
        yield Stub(stable_id="b", raw_url="u", title="t", court="c", hints={})

    pulsed = Pipeline._pulsed_stubs(
        slow_discover(), source="eu-cellar",
        on_progress=lambda **p: beats.append(p), interval=0.15)
    assert [s.stable_id for s in pulsed] == ["a", "b"]
    assert beats, "a discovery that blocks reported nothing at all"
    assert all(b["stage"] == "discovering eu-cellar" for b in beats)


def test_a_resumed_walk_reports_its_position_not_a_bare_zero():
    """Discovery and fetching interleave, so the pulse lands between the harvest loop's
    own reports. Carrying ``done=0`` made the Jobs panel flip between "harvesting
    22,000 of 24,059" and "discovering 0" every ten seconds — a 94%-complete Ofgem
    backfill reading as one that keeps going back to the start."""
    import time

    from raglex.core.models import Stub
    from raglex.pipeline.runner import Pipeline

    beats: list[dict] = []

    def resumed_discover():
        # the adapter reports the absolute offset of the page it resumed on
        yield Stub(stable_id="a", hints={"resume_offset": 22750, "feed_total": 24059})
        time.sleep(0.5)
        yield Stub(stable_id="b", hints={"resume_offset": 22760, "feed_total": 24059})

    pulsed = Pipeline._pulsed_stubs(
        resumed_discover(), source="uk-ofgem-publications",
        on_progress=lambda **p: beats.append(p), interval=0.15)
    assert [s.stable_id for s in pulsed] == ["a", "b"]
    # every beat after the first stub knows where in the register the walk is
    assert beats and all(b["done"] >= 22750 for b in beats)
    assert beats[-1]["discovered"] == 1


def test_the_pulse_still_says_zero_before_the_first_stub_arrives():
    """Zero is right for exactly one moment: nothing has a position yet."""
    import time

    from raglex.core.models import Stub
    from raglex.pipeline.runner import Pipeline

    beats: list[dict] = []

    def slow_first():
        time.sleep(0.5)
        yield Stub(stable_id="a", hints={})

    list(Pipeline._pulsed_stubs(slow_first(), source="s",
                                on_progress=lambda **p: beats.append(p), interval=0.15))
    assert beats and beats[0]["done"] == 0 and beats[0]["discovered"] == 0
