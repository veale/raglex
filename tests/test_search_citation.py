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


# -- relevance: how well the title matches, before how recent it is ----------
def _seed_ranking(facade):
    """One corpus, four documents whose titles all satisfy the query "code of practice",
    seeded so that DATE order and MATCH order disagree completely."""
    from raglex.core.models import RelationshipType, ResolutionStatus, TypedRelation

    with facade._open() as (cat, _rs, _ts):
        def doc(stable_id, title, when, source="eu-user-import"):
            cat.upsert_document(Record(
                source=source, stable_id=stable_id, doc_type=DocType.GUIDANCE,
                title=title, decision_date=when, extracted_via=ExtractedVia.MANUAL))
        # newest, but the worst match — a passing phrase inside a longer title
        doc("au/esafety/1", "Consolidated Industry Codes of Practice for the Online Industry",
            date(2026, 9, 9))
        doc("eu/opinion/1", "Commission Opinion on the assessment of the Code of Practice",
            date(2026, 7, 9))
        # oldest, but the exact thing someone typing the query is looking for
        doc("user:guidance:exact", "Code of Practice", date(2025, 1, 1))
        doc("user:guidance:prefix", "Code of Practice for General-Purpose AI Models",
            date(2025, 1, 1))
        cat.commit()


def test_relevance_beats_recency_for_a_title_query(tmp_path):
    """The bug this fixes: a title query was ordered by date, so a worse match published
    later outranked the document actually named by the query."""
    facade = Facade(_config(tmp_path))
    _seed_ranking(facade)
    ids = [i["stable_id"] for i in
           facade.search_corpus(query="code of practice", facets=False)["items"]]
    # exact title first, then the title that starts with it, then the merely-containing
    assert ids[:2] == ["user:guidance:exact", "user:guidance:prefix"]
    assert set(ids[2:]) == {"eu/opinion/1", "au/esafety/1"}


def test_citation_count_breaks_a_relevance_tie(tmp_path):
    """Two equally-good title matches are not equally what you meant — the one the corpus
    leans on wins, and only then the more recent."""
    facade = Facade(_config(tmp_path))
    with facade._open() as (cat, _rs, _ts):
        for n, when in ((1, date(2025, 1, 1)), (2, date(2026, 1, 1))):
            cat.upsert_document(Record(
                source="eu-user-import", stable_id=f"code/{n}", doc_type=DocType.GUIDANCE,
                title="Code of Practice", decision_date=when,
                extracted_via=ExtractedVia.MANUAL))
        # the OLDER one is the one everything cites
        cat.conn.execute(
            "INSERT INTO citation_counts (candidate_id, occurrences, rebuilt_at) "
            "VALUES (?, ?, ?)", ("code/1", 42, "2026-01-01T00:00:00"))
        cat.commit()
    ids = [i["stable_id"] for i in
           facade.search_corpus(query="code of practice", facets=False)["items"]]
    assert ids == ["code/1", "code/2"]


def test_a_bare_browse_is_still_newest_first(tmp_path):
    """Relevance only applies to a query. With nothing typed there is nothing to be
    relevant to, and the corpus browse must not change."""
    facade = Facade(_config(tmp_path))
    _seed_ranking(facade)
    res = facade.search_corpus(doc_type="guidance", facets=False)
    assert res["sort"] == "date"
    assert [i["stable_id"] for i in res["items"]][:2] == ["au/esafety/1", "eu/opinion/1"]


def test_an_explicit_sort_still_wins(tmp_path):
    facade = Facade(_config(tmp_path))
    _seed_ranking(facade)
    res = facade.search_corpus(query="code of practice", sort="date", facets=False)
    assert res["sort"] == "date"
    assert [i["stable_id"] for i in res["items"]][0] == "au/esafety/1"
