"""End-to-end pipeline tests with an in-memory fake adapter (no network)."""

from __future__ import annotations

from datetime import date
from typing import Iterator

from raglex.core.adapter import BaseAdapter
from raglex.core.errors import RateLimitException
from raglex.core.models import DocType, ExtractedVia, Record, Stub
from raglex.pipeline import Pipeline


class FakeAdapter(BaseAdapter):
    source = "fake"
    min_interval = 0.0

    def __init__(self, records: list[Record], *, rate_limit_after: int | None = None):
        self._records = records
        self._rate_limit_after = rate_limit_after
        self._fetched = 0

    def discover(self, since, *, max_pages=None) -> Iterator[Stub]:
        for rec in self._records:
            if since and rec.decision_date and rec.decision_date.isoformat() <= since:
                continue
            yield Stub(
                stable_id=rec.stable_id,
                title=rec.title,
                court=rec.court,
                hint_date=rec.decision_date,
            )

    def fetch(self, stub: Stub) -> Record | None:
        if self._rate_limit_after is not None and self._fetched >= self._rate_limit_after:
            raise RateLimitException(self.source)
        self._fetched += 1
        return next(r for r in self._records if r.stable_id == stub.stable_id)


def _rec(stable_id, text, court="uksc", d=date(2024, 1, 1), raw=None) -> Record:
    rec = Record(
        source="fake",
        stable_id=stable_id,
        doc_type=DocType.JUDGMENT,
        title=stable_id,
        court=court,
        decision_date=d,
        text=text,
        raw_bytes=raw if raw is not None else text.encode(),
        raw_ext="xml",
        extracted_via=ExtractedVia.STRUCTURED,
    )
    rec.ensure_payload_hash()
    return rec


def test_pipeline_stores_everything_no_topic_gate(catalogue, rawstore):
    # Generic service: no topic filtering — every discovered/fetched document is stored.
    records = [
        _rec("a", "This concerns personal data and the GDPR 2016/679."),
        _rec("b", "A contract dispute about bricks and mortar."),
    ]
    pipe = Pipeline(catalogue, rawstore)
    stats = pipe.run(FakeAdapter(records))

    assert stats.stored == 2
    assert catalogue.get_document("a") is not None
    assert catalogue.get_document("b") is not None


def test_pipeline_content_hash_dedup(catalogue, rawstore):
    rec = _rec("a", "personal data GDPR 2016/679 data protection", d=date(2024, 1, 1))
    pipe = Pipeline(catalogue, rawstore)
    pipe.run(FakeAdapter([rec]))
    # identical bytes, later date so it passes the watermark -> deduped on hash
    dup = _rec("a-copy", "personal data GDPR 2016/679 data protection", d=date(2024, 2, 1))
    stats = pipe.run(FakeAdapter([dup]))
    assert stats.deduped == 1
    assert stats.stored == 0


def test_pipeline_mints_celex_to_ecli_alias(catalogue, rawstore):
    """Every harvest of an ECLI-keyed doc carrying a CELEX mints the CELEX→ECLI
    alias, so EU case-number citations resolve systematically (rec 3, §5b)."""
    rec = Record(
        source="eu-cellar", stable_id="ECLI:EU:C:2020:559", ecli="ECLI:EU:C:2020:559",
        doc_type=DocType.JUDGMENT, decision_date=date(2024, 1, 1),
        text="personal data GDPR 2016/679", raw_bytes=b"judgment",
        extra={"celex": "62018CJ0311"},
    )
    rec.ensure_payload_hash()
    Pipeline(catalogue, rawstore).run(FakeAdapter([rec]))
    assert catalogue.get_alias("62018cj0311") == "ECLI:EU:C:2020:559"


def test_pipeline_mints_chamberless_alias(catalogue, rawstore):
    """Harvesting ukut/aac/2012/440 mints the chamber-less alias ukut/2012/440, so a
    citation that omits the chamber resolves to it (§5b)."""
    rec = _rec("ukut/aac/2012/440", "personal data GDPR 2016/679", court="ukut")
    Pipeline(catalogue, rawstore).run(FakeAdapter([rec]))
    assert catalogue.get_alias("ukut/2012/440") == "ukut/aac/2012/440"
    # find_document_id resolves the bare form via the alias
    assert catalogue.find_document_id("ukut/2012/440") == "ukut/aac/2012/440"


def test_record_extra_metadata_is_persisted(catalogue, rawstore):
    rec = _rec("ECLI:EU:C:2020:559", "A CJEU judgment.", court="cjeu")
    rec.extra = {"celex": "62018CJ0311", "origin_country": "United Kingdom",
                 "referring_courts": ["Upper Tribunal"]}
    Pipeline(catalogue, rawstore).run(FakeAdapter([rec]))
    meta = catalogue.document_meta("ECLI:EU:C:2020:559")
    assert meta["origin_country"] == "United Kingdom"
    assert meta["referring_courts"] == ["Upper Tribunal"]


def test_meta_json_migration_is_idempotent(catalogue):
    # _migrate() runs on every init; calling again must not error or duplicate
    catalogue._migrate()
    cols = {r["name"] for r in catalogue.conn.execute("PRAGMA table_info(documents)")}
    assert "meta_json" in cols


def test_pipeline_records_outstanding_effects_queue(catalogue, rawstore):
    rec = _rec("ukpga/2018/12", "Data protection law.", court=None)
    rec.doc_type = DocType.LEGISLATION
    rec.extra = {"unapplied_effects": {"outstanding": 2, "affecting": ["ukpga/2025/8"]}}
    Pipeline(catalogue, rawstore).run(FakeAdapter([rec]))
    rows = catalogue.list_effects_refresh()
    assert [(r["stable_id"], r["outstanding"]) for r in rows] == [("ukpga/2018/12", 2)]
    # a later re-pull with everything incorporated drops it from the queue (the
    # refresh worker uses backfill=True to bypass the watermark, as here)
    rec.extra = {"unapplied_effects": {"outstanding": 0, "affecting": []}}
    Pipeline(catalogue, rawstore).run(FakeAdapter([rec]), backfill=True)
    assert catalogue.list_effects_refresh() == []


def test_pipeline_watermark_advances_on_clean_run(catalogue, rawstore):
    rec = _rec("a", "personal data GDPR 2016/679", d=date(2024, 5, 1))
    pipe = Pipeline(catalogue, rawstore)
    pipe.run(FakeAdapter([rec]))
    assert catalogue.get_watermark("fake") == "2024-05-01"


def test_rate_limit_pauses_without_advancing_watermark(catalogue, rawstore):
    records = [
        _rec("a", "personal data GDPR 2016/679", d=date(2024, 5, 1)),
        _rec("b", "personal data GDPR 2016/679", d=date(2024, 6, 1)),
    ]
    pipe = Pipeline(catalogue, rawstore)
    stats = pipe.run(FakeAdapter(records, rate_limit_after=1))
    assert stats.rate_limited is True
    # one stored before the wall; watermark NOT advanced so the run resumes (§5a)
    assert stats.stored == 1
    assert catalogue.get_watermark("fake") is None


class HintedAdapter(BaseAdapter):
    """Adapter whose stubs carry feed hints (full-timestamp watermark, contenthash) —
    the Find Case Law shape."""

    source = "fake"
    min_interval = 0.0

    def __init__(self, records, hints_by_id=None):
        self._records = records
        self._hints = hints_by_id or {}
        self.fetched: list[str] = []

    def discover(self, since, *, max_pages=None) -> Iterator[Stub]:
        for rec in self._records:
            yield Stub(stable_id=rec.stable_id, title=rec.title, court=rec.court,
                       hint_date=rec.decision_date, hints=self._hints.get(rec.stable_id, {}))

    def fetch(self, stub: Stub) -> Record | None:
        self.fetched.append(stub.stable_id)
        return next(r for r in self._records if r.stable_id == stub.stable_id)


def test_watermark_prefers_full_timestamp_hint(catalogue, rawstore):
    rec = _rec("a", "personal data GDPR 2016/679")
    pipe = Pipeline(catalogue, rawstore)
    pipe.run(HintedAdapter([rec], {"a": {"watermark": "2024-01-01T15:30:00+00:00"}}))
    # the cursor keeps the same-day TIME — a date-only cursor loses same-day arrivals
    assert catalogue.get_watermark("fake") == "2024-01-01T15:30:00+00:00"


def test_watermark_key_scopes_the_cursor(catalogue, rawstore):
    rec = _rec("a", "personal data GDPR 2016/679")
    pipe = Pipeline(catalogue, rawstore)
    pipe.run(HintedAdapter([rec]), watermark_key="watch:7:fake")
    # the watch's cursor advanced; the source-wide cursor is untouched
    assert catalogue.get_watermark("watch:7:fake") == "2024-01-01"
    assert catalogue.get_watermark("fake") is None


def test_contenthash_change_refetches_held_document(catalogue, rawstore):
    v1 = _rec("a", "personal data GDPR 2016/679 version one")
    pipe = Pipeline(catalogue, rawstore)
    v1.extra["contenthash"] = "hash-1"
    pipe.run(HintedAdapter([v1], {"a": {"contenthash": "hash-1"}}))

    # same hash → dedup before fetch (no needless download)
    ad = HintedAdapter([v1], {"a": {"contenthash": "hash-1"}})
    stats = pipe.run(ad)
    assert ad.fetched == [] and stats.deduped == 1

    # changed hash → the held copy is a superseded revision → re-fetch + flag refreshed
    v2 = _rec("a", "personal data GDPR 2016/679 version two REVISED")
    v2.extra["contenthash"] = "hash-2"
    ad2 = HintedAdapter([v2], {"a": {"contenthash": "hash-2"}})
    stats2 = pipe.run(ad2)
    assert ad2.fetched == ["a"]
    assert stats2.refreshed_ids == ["a"]
    assert catalogue.document_meta("a")["contenthash"] == "hash-2"


def test_one_malformed_document_does_not_sink_the_run(catalogue, rawstore):
    """A parser blowing up on a corrupt file (e.g. PyMuPDF's "Failed to open stream")
    must not abort the crawl and lose every item after it — it's a transient item error."""
    class _BoomAdapter(FakeAdapter):
        def fetch(self, stub):
            if stub.stable_id == "bad":
                raise RuntimeError("Failed to open stream")
            return super().fetch(stub)

    good1 = _rec("a", "personal data GDPR 2016/679 data protection")
    bad = _rec("bad", "personal data GDPR 2016/679 data protection two")
    good2 = _rec("b", "personal data GDPR 2016/679 data protection three")
    stats = Pipeline(catalogue, rawstore).run(_BoomAdapter([good1, bad, good2]))

    # the run continued past the bad document and stored the ones after it
    assert catalogue.get_document("a") is not None
    assert catalogue.get_document("b") is not None
    assert catalogue.get_document("bad") is None
    assert stats.stored == 2
    # counted as a TRANSIENT item error (unknown cause → retry, never "this doesn't exist")
    assert stats.errors == 1 and stats.errors_transient == 1 and stats.errors_fatal == 0
    # and it says which document blew up, and how
    assert any("bad" in n and "Failed to open stream" in n for n in stats.notes)
