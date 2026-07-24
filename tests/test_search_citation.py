"""Corpus search matches by title AND by citation form (id / ECLI / folded alias), and the
hybrid engine skips the vector half when no ANN index exists (so it can't seq-scan a huge
embeddings table). Backend-agnostic (SQLite here; the pg trigram indexes make the same
predicates index-backed in production)."""

from __future__ import annotations

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
