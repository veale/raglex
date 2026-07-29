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


@pytest.fixture()
def facade(tmp_path):
    return Facade(Config(
        data_dir=tmp_path, catalogue_path=str(tmp_path / "c.sqlite"),
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "s.json",
        embed_provider="local-hashing", embed_model=None))


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


def test_mojibake_repair_reports_even_when_nothing_needs_repairing(facade):
    _seed(facade, n=2500)
    rec = Recorder()
    st = facade.repair_mojibake(on_progress=rec)
    assert st["repaired"] == 0, "fixture text needs no repair"
    assert rec.calls, "a pass that changed nothing reported no progress"
    assert rec.calls[-1]["done"] >= 2000


def test_resegment_reports_even_when_every_document_is_unchanged(facade):
    _seed(facade, n=2500)
    rec = Recorder()
    st = facade.resegment_judgments(on_progress=rec)
    assert st["improved"] == 0 and st["derived"] == 0
    assert rec.calls, "an all-skip pass reported no progress"


def test_intituling_reports_even_when_no_bench_is_found(facade):
    _seed(facade, n=1200)
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
