"""The pipeline's batched held-prefilter — a resume walk over a mostly-held source
must cost one IN-query per batch, not one point SELECT per stub, while keeping every
dedup decision identical (id match, URL fallback, unextracted carry-forward)."""

from __future__ import annotations

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record, Stub
from raglex.facade import Facade
from raglex.pipeline import Pipeline


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite",
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json", embed_provider="local-hashing",
        embed_model=None,
    )


class _FakeAdapter:
    source = "fake"
    min_interval = 0.0
    requires_js = False
    requires_proxy = False

    def __init__(self, stubs):
        self._stubs = stubs
        self.fetched: list[str] = []

    def discover(self, since, *, max_pages=None):
        yield from self._stubs

    def fetch(self, stub):
        self.fetched.append(stub.stable_id)
        return Record(source=self.source, stable_id=stub.stable_id,
                      doc_type=DocType.JUDGMENT, title=stub.stable_id,
                      text=f"body of {stub.stable_id}",
                      extracted_via=ExtractedVia.STRUCTURED)


def test_resume_walk_dedups_in_batches_and_fetches_only_the_new(tmp_path, monkeypatch):
    facade = Facade(_config(tmp_path))
    held = []
    with facade._open() as (cat, rs, ts):
        for i in range(230):                       # > one prefilter batch
            rec = Record(source="fake", stable_id=f"fake/doc/{i}",
                         doc_type=DocType.JUDGMENT, title=f"D{i}",
                         text=f"text {i}", extracted_via=ExtractedVia.STRUCTURED)
            rec.ensure_payload_hash()
            ts.put(rec.payload_hash, rec.text)
            cat.upsert_document(rec)
            held.append(rec.stable_id)
        # half the held docs are stamped extracted; the rest must be carried into
        # the extraction backlog on dedup
        for sid in held[:100]:
            cat.mark_extracted(sid, commit=False)
        cat.commit()

        calls = {"state": 0, "urls": 0}
        orig_state = cat.held_extraction_state
        orig_urls = cat.document_ids_by_landing_urls
        monkeypatch.setattr(cat, "held_extraction_state",
                            lambda ids: (calls.__setitem__("state", calls["state"] + 1),
                                         orig_state(ids))[1])
        monkeypatch.setattr(cat, "document_ids_by_landing_urls",
                            lambda urls: (calls.__setitem__("urls", calls["urls"] + 1),
                                          orig_urls(urls))[1])

        stubs = [Stub(stable_id=sid) for sid in held] + \
                [Stub(stable_id=f"fake/new/{i}") for i in range(5)]
        adapter = _FakeAdapter(stubs)
        stats = Pipeline(cat, rs, textstore=ts).run(adapter, record_health=False)

        assert stats.deduped == 230
        assert stats.stored == 5
        assert adapter.fetched == [f"fake/new/{i}" for i in range(5)]
        # the 130 held-but-unstamped docs ride into the extraction backlog,
        # alongside the 5 genuinely new ones
        assert sum(1 for sid in stats.stored_ids if sid.startswith("fake/doc/")) == 130
        # 235 stubs → 2 batched state queries (batch=200), never 230 point lookups
        assert calls["state"] == 2


def test_alias_rung_dedups_upstream_surrogate_ids(tmp_path):
    """The de-rii pattern: the held decision is keyed by ECLI, the resume walk's
    stubs by the upstream doknr. With the doknr minted as an alias, the prefilter's
    alias rung dedups WITHOUT reading the file (adapter.fetch never called)."""
    facade = Facade(_config(tmp_path))
    with facade._open() as (cat, rs, ts):
        rec = Record(source="fake", stable_id="ECLI:DE:BGH:2020:XYZ",
                     doc_type=DocType.JUDGMENT, title="Real", text="t",
                     extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        ts.put(rec.payload_hash, rec.text)
        cat.upsert_document(rec)
        cat.mark_extracted(rec.stable_id)
        cat.put_alias("jb-KORE609062024", rec.stable_id, source="adapter-alias")

        adapter = _FakeAdapter([Stub(stable_id="jb-KORE609062024")])
        stats = Pipeline(cat, rs, textstore=ts).run(adapter, record_health=False)
        assert stats.deduped == 1 and stats.stored == 0
        assert adapter.fetched == []


def test_url_fallback_still_dedups_provisional_ids(tmp_path):
    """An adapter whose stub id is provisional (NZ pattern): the held document is
    keyed by its real citation but shares the landing URL — batched prefilter must
    still dedup it via the URL map."""
    facade = Facade(_config(tmp_path))
    with facade._open() as (cat, rs, ts):
        rec = Record(source="fake", stable_id="nzsc/2020/1", doc_type=DocType.JUDGMENT,
                     title="Real", text="t", landing_url="https://x/case-1",
                     extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        ts.put(rec.payload_hash, rec.text)
        cat.upsert_document(rec)
        cat.mark_extracted("nzsc/2020/1")

        adapter = _FakeAdapter([Stub(stable_id="prov-1", landing_url="https://x/case-1")])
        stats = Pipeline(cat, rs, textstore=ts).run(adapter, record_health=False)
        assert stats.deduped == 1 and stats.stored == 0
        assert adapter.fetched == []


class _RekeyAdapter:
    """A provisional-id adapter (Ireland/NZ pattern) whose stub id can't be matched
    against a bulk-seeded copy until fetch() reveals the real neutral-cite id."""

    source = "rekey"
    min_interval = 0.0
    requires_js = False
    requires_proxy = False

    def __init__(self, stubs, real_ids):
        self._stubs = stubs
        self._real = real_ids  # stub id → real id fetch() mints
        self.fetched: list[str] = []

    def discover(self, since, *, max_pages=None):
        yield from self._stubs

    def fetch(self, stub):
        self.fetched.append(stub.stable_id)
        rid = self._real[stub.stable_id]
        return Record(source=self.source, stable_id=rid, doc_type=DocType.JUDGMENT,
                      title=rid, text=f"native courts.ie body of {rid}",
                      extracted_via=ExtractedVia.STRUCTURED)


def test_provisional_id_dedups_after_fetch_reveals_bulk_seeded_id(tmp_path):
    """A wrong/absent filename slug slips the pre-filter, so the case IS fetched; but once
    fetch() mints the real neutral-cite id and we already hold it (from a bulk import), the
    runner must NOT store — otherwise it would archive the held copy and supersede it with
    this source's copy on every backfill. A genuinely new case still stores."""
    facade = Facade(_config(tmp_path))
    with facade._open() as (cat, rs, ts):
        # a bulk-seeded case, keyed by its neutral citation, with DIFFERENT payload bytes
        bulk = Record(source="ie-bulk", stable_id="iesc/2025/49", doc_type=DocType.JUDGMENT,
                      title="Bulk copy", text="bulk-import body (different bytes)",
                      extracted_via=ExtractedVia.STRUCTURED)
        bulk.ensure_payload_hash()
        ts.put(bulk.payload_hash, bulk.text)
        cat.upsert_document(bulk)
        cat.mark_extracted("iesc/2025/49")
        before = cat.get_document("iesc/2025/49")

        stubs = [Stub(stable_id="ie-caselaw/uuid-A"),   # filename unparseable → provisional
                 Stub(stable_id="iehc/2026/999")]       # a genuinely new case
        adapter = _RekeyAdapter(
            stubs, {"ie-caselaw/uuid-A": "iesc/2025/49", "iehc/2026/999": "iehc/2026/999"})
        stats = Pipeline(cat, rs, textstore=ts).run(adapter, record_health=False)

        # both were fetched (the pre-filter couldn't match the provisional ids)…
        assert adapter.fetched == ["ie-caselaw/uuid-A", "iehc/2026/999"]
        # …but only the genuinely-new case was stored; the bulk-seeded one was deduped
        assert stats.stored == 1 and stats.deduped == 1
        assert cat.get_document("iehc/2026/999") is not None
        # the held bulk copy is untouched — same source, same version (not superseded)
        after = cat.get_document("iesc/2025/49")
        assert after["source"] == "ie-bulk"
        assert after["version"] == before["version"]
