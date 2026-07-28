"""Corpus search matches by title AND by citation form (id / ECLI / folded alias), and the
hybrid engine skips the vector half when no ANN index exists (so it can't seq-scan a huge
embeddings table). Backend-agnostic (SQLite here; the pg trigram indexes make the same
predicates index-backed in production)."""

from __future__ import annotations

from datetime import date

from raglex.config import Config
from raglex.core.models import DocType, ExtractedVia, Record
from raglex.facade import Facade


def _config(tmp_path) -> Config:
    return Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite",
        raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
        settings_path=tmp_path / "settings.json", embed_provider="local-hashing",
        embed_model=None,
    )


def _seed(facade):
    with facade._open() as (cat, _rs, _ts):
        cat.upsert_document(Record(
            source="ie-caselaw", stable_id="iesc/2011/26", ecli="ECLI:IE:SC:2011:26",
            doc_type=DocType.JUDGMENT, title="Murphy v Data Protection Commissioner",
            extracted_via=ExtractedVia.STRUCTURED))
        # the human citation forms are stored folded (lower-case) as aliases → dst_id
        cat.put_alias("[2011] iesc 26", "iesc/2011/26", source="test")
        cat.put_alias("[2011] 3 ir 1", "iesc/2011/26", source="test")
        cat.commit()


def _ids(res):
    return {i["stable_id"] for i in res["items"]}


def test_search_by_title(tmp_path):
    facade = Facade(_config(tmp_path))
    _seed(facade)
    # non-consecutive title words both match
    assert "iesc/2011/26" in _ids(facade.search_corpus(query="data protection", facets=False))


def test_search_by_neutral_citation_format(tmp_path):
    facade = Facade(_config(tmp_path))
    _seed(facade)
    # typing the neutral citation as written ("[2011] IESC 26") finds it via the folded alias
    assert "iesc/2011/26" in _ids(facade.search_corpus(query="[2011] IESC 26", facets=False))


def test_search_by_report_citation_alias(tmp_path):
    facade = Facade(_config(tmp_path))
    _seed(facade)
    assert "iesc/2011/26" in _ids(facade.search_corpus(query="[2011] 3 IR 1", facets=False))


def test_search_by_ecli(tmp_path):
    facade = Facade(_config(tmp_path))
    _seed(facade)
    assert "iesc/2011/26" in _ids(facade.search_corpus(query="ECLI:IE:SC:2011:26", facets=False))


def test_search_by_slug_id(tmp_path):
    facade = Facade(_config(tmp_path))
    _seed(facade)
    assert "iesc/2011/26" in _ids(facade.search_corpus(query="iesc/2011/26", facets=False))


def test_no_false_match(tmp_path):
    facade = Facade(_config(tmp_path))
    _seed(facade)
    assert _ids(facade.search_corpus(query="entirely unrelated phrase", facets=False)) == set()


# ── semantic gating ───────────────────────────────────────────────────────────
def test_search_engine_skips_vector_when_no_ann_index(monkeypatch):
    from raglex.retrieval import search as search_mod
    from raglex.retrieval.search import SearchEngine

    class _Cat:
        def has_vector_index(self, dims=None):
            return False

    class _Prov:
        name, model, model_version, dimensions = "p", "m", "v", 256

    called = {"vec": 0, "fts": 0}
    monkeypatch.setattr(search_mod, "vector_search",
                        lambda *a, **k: called.__setitem__("vec", called["vec"] + 1) or [])
    monkeypatch.setattr(search_mod, "fts_search",
                        lambda *a, **k: called.__setitem__("fts", called["fts"] + 1) or [])
    monkeypatch.setattr(SearchEngine, "_assemble", lambda self, *a, **k: None)
    eng = SearchEngine(_Cat(), _Prov())
    eng.catalogue.authority_for = lambda ids: {}
    eng.search("anything", k=5)
    assert called["vec"] == 0  # auto-gated OFF: no ANN index → no vector seq-scan
    assert called["fts"] == 1  # lexical half still runs


def test_search_engine_runs_vector_when_forced_on(monkeypatch):
    from raglex.retrieval import search as search_mod
    from raglex.retrieval.search import SearchEngine

    class _Cat:
        def has_vector_index(self, dims=None):
            return False

    class _Prov:
        name, model, model_version, dimensions = "p", "m", "v", 256

    called = {"vec": 0}
    monkeypatch.setattr(search_mod, "vector_search",
                        lambda *a, **k: called.__setitem__("vec", called["vec"] + 1) or [])
    monkeypatch.setattr(search_mod, "fts_search", lambda *a, **k: [])
    monkeypatch.setattr(SearchEngine, "_assemble", lambda self, *a, **k: None)
    eng = SearchEngine(_Cat(), _Prov())
    eng.catalogue.authority_for = lambda ids: {}
    eng.search("anything", k=5, semantic=True)
    assert called["vec"] == 1  # explicit override runs it despite no index


def test_search_finds_a_case_by_the_name_it_is_known_by_not_titled_with(tmp_path):
    """"Dun & Bradstreet Austria" is what everyone calls CK v Magistrat der Stadt Wien —
    it is in the case's "also cited as" line, not its title, so title search alone could
    never find it by the only name most readers know."""
    f = Facade(_config(tmp_path))
    with f._open() as (cat, _rs, ts):
        rec = Record(source="eu-cellar", stable_id="ECLI:EU:C:2025:117",
                     ecli="ECLI:EU:C:2025:117", doc_type=DocType.JUDGMENT,
                     title="CK v Magistrat der Stadt Wien", decision_date=date(2025, 2, 27),
                     text="Judgment on automated decision-making.",
                     raw_bytes=b"x", extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, rec.text)))
        cat.put_alias("dun & bradstreet austria", "ECLI:EU:C:2025:117", source="cellar-alias")
        cat.commit()

    hits = f.search_corpus(query="Dun & Bradstreet Austria")
    assert [d["stable_id"] for d in hits["items"]] == ["ECLI:EU:C:2025:117"]
    # the title route still works, and an unrelated name still finds nothing
    assert [d["stable_id"] for d in f.search_corpus(query="Magistrat Wien")["items"]] == [
        "ECLI:EU:C:2025:117"]
    assert f.search_corpus(query="Bradstreet Norway")["items"] == []
