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


def test_autocomplete_prefixes_a_learned_shorthand_and_explains_the_hit(tmp_path):
    """A person sees CPIA while typing CPI, even though those letters are absent from
    the Act's title; stored grammatical "the CPIA" forms are equivalent too."""
    from raglex.citations.extractor import SHORTHAND_MIN_DOCS

    f = Facade(_config(tmp_path))
    with f._open() as (cat, _rs, _ts):
        cat.upsert_document(Record(
            source="uk-legislation", stable_id="ukpga/1996/25",
            doc_type=DocType.LEGISLATION,
            title="Criminal Procedure and Investigations Act 1996",
            extracted_via=ExtractedVia.STRUCTURED))
        for i in range(SHORTHAND_MIN_DOCS):
            cat.add_learned_shorthands([
                {"shorthand": "the CPIA", "candidate_id": "ukpga/1996/25",
                 "entity_kind": "act", "is_abbrev": True}], doc_id=f"case/{i}")
        cat.commit()

    hit = f.search_corpus(query="CPI", facets=False)
    assert [r["stable_id"] for r in hit["items"]] == ["ukpga/1996/25"]
    assert hit["items"][0]["matched_shorthand"] == "the CPIA"


def test_search_corpus_jurisdiction_filter_uses_explore_buckets(tmp_path):
    """OSS decisions inherit their country from dpa-xx, not their EU source."""
    f = Facade(_config(tmp_path))
    with f._open() as (cat, _rs, _ts):
        cat.upsert_document(Record(
            source="eu-dpa", stable_id="eu/ordinary", doc_type=DocType.DECISION,
            title="Common register decision", extracted_via=ExtractedVia.STRUCTURED))
        cat.upsert_document(Record(
            source="eu-dpa", stable_id="fr/oss", court="dpa-fr",
            doc_type=DocType.DECISION, title="Common register decision",
            extracted_via=ExtractedVia.STRUCTURED))
        cat.commit()

    france = f.search_corpus(query="common register", jurisdiction="France", facets=False)
    eu = f.search_corpus(query="common register", jurisdiction="European Union", facets=False)
    assert [r["stable_id"] for r in france["items"]] == ["fr/oss"]
    assert [r["stable_id"] for r in eu["items"]] == ["eu/ordinary"]


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


def test_primary_authority_beats_commentary_with_the_same_title_match(tmp_path):
    f = Facade(_config(tmp_path))
    with f._open() as (cat, _rs, _ts):
        for sid, kind in (("act/original", DocType.LEGISLATION),
                          ("note/commentary", DocType.COMMENTARY)):
            cat.upsert_document(Record(
                source="uk-user-import", stable_id=sid, doc_type=kind,
                title="Example Act 2025 — overview", decision_date=date(2025, 1, 1),
                extracted_via=ExtractedVia.MANUAL))
        # Popularity helps within a class, but should not let derivative material crowd
        # the authority itself out of a short suggestion list.
        cat.conn.execute(
            "INSERT INTO citation_counts (candidate_id, occurrences, rebuilt_at) "
            "VALUES (?, ?, ?)", ("note/commentary", 1000, "2026-01-01T00:00:00"))
        cat.commit()
    ids = [r["stable_id"] for r in
           f.search_corpus(query="Example Act", facets=False)["items"]]
    assert ids == ["act/original", "note/commentary"]


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


# -- a pinpoint's subdivision: found, or only its parent? --------------------
def test_a_missing_subdivision_is_reported_not_asserted(tmp_path):
    """Anchor keys fold to unit+number, so "s. 7(99)" matches the s. 7 segment. Returning
    the parent is the right ANSWER — sections are rarely segmented below the section —
    but it must not come back looking like an exact hit, which is how a bogus pinpoint
    became indistinguishable from a real one."""
    from raglex.core.models import Segment
    from raglex.facade import _match_segment, _subdivision_note

    text = "s. 7 Right of access to personal data.\nAn individual is entitled under (1)..."
    segs = [Segment(label="s. 7 Right of access to personal data.",
                    char_start=0, char_end=len(text), kind="section")]
    # the subdivision IS in the provision's text → an exact match, no note
    assert _subdivision_note("s. 7(1)", segs[_match_segment(segs, "s. 7(1)")], text) is None
    # a bare section asks for no subdivision at all
    assert _subdivision_note("s. 7", segs[_match_segment(segs, "s. 7")], text) is None
    # …and one that is nowhere in it says so
    note = _subdivision_note("s. 7(99)", segs[_match_segment(segs, "s. 7(99)")], text)
    assert note and "(99)" in note and "parent provision" in note
