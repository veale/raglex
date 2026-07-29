"""Searching: literal verification, facets over the whole result set, and the
network view of a set of results.

The SQLite backend has no tsvector, so the index-side tests live against the
in-memory catalogue only where they are backend-neutral; the verification and
faceting logic — which is where the correctness lives — is pure Python and is
tested directly.
"""

from __future__ import annotations

from raglex.fulltext.index import (
    find_literal, highlight_spans, snippet, verify,
)
from raglex.fulltext.query import Phrase, parse


# -- literal verification ------------------------------------------------------
def test_a_quoted_phrase_means_the_characters_not_the_stems():
    """The whole point. Postgres retrieves both of these for "duty of care";
    only the text can tell them apart."""
    p = parse('"duty of care"')
    assert verify("the defendant owed a duty of care", p) is not None
    assert verify("he owed duties of care throughout", p) is None


def test_relaxed_mode_asks_for_no_verification():
    p = parse('"duty of care"', exact=False)
    # nothing to check — the tsquery already decided, stems and all
    assert p.literals == []
    assert verify("he owed duties of care throughout", p) == 0


def test_verification_survives_the_damage_extraction_leaves():
    p = parse('"duty of care"')
    for text in ("a duty\nof care", "a duty  of   care", "a duty of\n  care"):
        assert verify(text, p) is not None, text


def test_a_word_boundary_is_respected():
    assert find_literal("he was careless", Phrase(["care"])) is None
    assert find_literal("he took care", Phrase(["care"])) is not None


def test_exclusion_is_applied_against_the_text():
    """-"contributory negligence" must drop the document that really contains it,
    and keep one that merely stems to it."""
    p = parse('"duty of care" -"contributory negligence"')
    assert verify("a duty of care; contributory negligence found", p) is None
    assert verify("a duty of care; contributorily negligent", p) is not None


def test_every_required_phrase_must_be_present():
    p = parse('"duty of care" "reasonable foreseeability"')
    assert verify("a duty of care alone", p) is None
    assert verify("a duty of care and reasonable foreseeability", p) is not None


# -- what the reader sees ------------------------------------------------------
def test_highlights_mark_the_phrase_not_its_words():
    p = parse('"duty of care"')
    frag = "the defendant owed a duty of care to the claimant"
    spans = highlight_spans(frag, p)
    assert [frag[a:b] for a, b in spans] == ["duty of care"]


def test_highlights_cover_every_searched_term():
    p = parse("negligence damages")
    frag = "the negligence caused damages and more damages"
    assert [frag[a:b] for a, b in highlight_spans(frag, p)] == \
        ["negligence", "damages", "damages"]


def test_a_prefix_term_highlights_the_whole_word_it_completes():
    p = parse("neglig*")
    frag = "found negligence on the facts"
    assert [frag[a:b] for a, b in highlight_spans(frag, p)] == ["negligence"]


def test_an_excluded_phrase_is_never_highlighted():
    p = parse('"duty of care" -negligence')
    frag = "a duty of care, not negligence"
    assert [frag[a:b] for a, b in highlight_spans(frag, p)] == ["duty of care"]


def test_overlapping_matches_merge_rather_than_nest():
    p = parse("care duty")
    frag = "duty care duty"
    spans = highlight_spans(frag, p)
    assert spans == sorted(spans) and all(a < b for a, b in spans)
    for i in range(1, len(spans)):
        assert spans[i][0] > spans[i - 1][1]


def test_snippet_windows_the_match_and_keeps_whole_words():
    text = "x " * 200 + "the duty of care arises here " + "y " * 200
    at = text.index("duty")
    frag = snippet(text, at)
    assert "duty of care" in frag and len(frag) < 400
    assert not frag.startswith(" ")


# -- facets --------------------------------------------------------------------
def _facets(facade, meta):
    return facade._freetext_facets(meta)


def test_facets_describe_every_match_not_the_page(tmp_path):
    """The bug this exists to avoid: a reader told "912 documents" and then shown a
    breakdown of the 40 that happened to be loaded."""
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config(data_dir=tmp_path, catalogue_path=str(tmp_path / "c.sqlite"),
                      raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                      settings_path=tmp_path / "s.json",
                      embed_provider="local-hashing", embed_model=None))
    meta = [
        {"source": "uk-caselaw", "court": "ewca", "doc_type": "judgment",
         "decision_date": "2015-04-02"},
        {"source": "uk-caselaw", "court": "ewhc", "doc_type": "judgment",
         "decision_date": "2015-09-11"},
        {"source": "uk-legislation", "court": None, "doc_type": "legislation",
         "decision_date": "1998-11-09"},
        {"source": "uk-caselaw", "court": "ewca", "doc_type": "judgment",
         "decision_date": None},
    ]
    fac = _facets(f, meta)
    assert {r["value"]: r["n"] for r in fac["source"]} == \
        {"uk-caselaw": 3, "uk-legislation": 1}
    assert {r["value"]: r["n"] for r in fac["doc_type"]} == \
        {"judgment": 3, "legislation": 1}
    assert {r["value"]: r["n"] for r in fac["court"]} == {"ewca": 2, "ewhc": 1}
    assert fac["years"] == [{"year": "1998", "n": 1}, {"year": "2015", "n": 2}]
    assert fac["undated"] == 1


def test_facets_of_an_empty_result_set_are_empty(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config(data_dir=tmp_path, catalogue_path=str(tmp_path / "c.sqlite"),
                      raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                      settings_path=tmp_path / "s.json",
                      embed_provider="local-hashing", embed_model=None))
    fac = _facets(f, [])
    assert fac["source"] == [] and fac["years"] == [] and fac["undated"] == 0


# -- the network view of a result set -----------------------------------------
def test_what_a_set_of_results_most_often_cites(catalogue):
    """The doctrinal anchors of a search: which authorities the matching documents
    have in common. No per-document view can show this."""
    from datetime import date

    from raglex.core.models import (
        DocType, ExtractedVia, Record, RelationshipType, ResolutionStatus,
        TypedRelation,
    )

    def doc(sid, cites):
        rec = Record(source="uk-caselaw", stable_id=sid, doc_type=DocType.JUDGMENT,
                     title=sid, decision_date=date(2020, 1, 1), text="t",
                     raw_bytes=b"t", extracted_via=ExtractedVia.STRUCTURED,
                     relations=[TypedRelation(
                         relationship_type=RelationshipType.MENTIONS, dst_id=c,
                         extracted_via=ExtractedVia.REGEX,
                         resolution_status=ResolutionStatus.RESOLVED) for c in cites])
        rec.ensure_payload_hash()
        catalogue.upsert_document(rec)

    doc("a", ["donoghue", "caparo"])
    doc("b", ["donoghue"])
    doc("c", ["donoghue", "caparo"])
    top = catalogue.cited_by_documents(["a", "b", "c"])
    assert [(t["stable_id"], t["citing"]) for t in top] == \
        [("donoghue", 3), ("caparo", 2)]
    # and it is scoped to the set asked about
    assert [t["stable_id"] for t in catalogue.cited_by_documents(["b"])] == ["donoghue"]


def test_the_network_view_of_nothing_is_nothing(catalogue):
    assert catalogue.cited_by_documents([]) == []


# -- the settings the Search page writes ---------------------------------------
def test_the_search_settings_are_registered(tmp_path):
    """SettingsStore.update silently ignores keys it doesn't know, so an
    unregistered setting saves without error and reads back empty — which is
    exactly what happened: the scope tick list and the reader-facing note both
    failed to persist, with nothing reported."""
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config(data_dir=tmp_path, catalogue_path=str(tmp_path / "c.sqlite"),
                      raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                      settings_path=tmp_path / "s.json",
                      embed_provider="local-hashing", embed_model=None))
    f.set_freetext_scope(sources=["uk-caselaw", "eu-cellar"], note="only the UK and EU")
    scope = f.freetext_scope()
    assert sorted(scope["selected"]) == ["eu-cellar", "uk-caselaw"]
    assert scope["note"] == "only the UK and EU"


def test_the_candidate_set_is_resolved_in_one_query_not_one_each(catalogue, tmp_path):
    """The first real search took 34 seconds. Reading a document is 0.046 ms; asking
    Postgres where it lives, once per candidate, is not — and there were 4,000 of
    them. The lookup is batched."""
    from datetime import date

    from raglex.core.models import DocType, ExtractedVia, Record
    from raglex.fulltext.index import _hashes_for
    from raglex.storage import TextStore

    ts = TextStore(tmp_path / "text")
    ids = []
    for i in range(50):
        rec = Record(source="uk-caselaw", stable_id=f"d{i}", doc_type=DocType.JUDGMENT,
                     title=f"doc {i}", decision_date=date(2020, 1, 1), text=f"body {i}",
                     raw_bytes=b"x", extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, f"body {i}")))
        ids.append(f"d{i}")

    calls = {"n": 0}

    class CountingConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **k):
            calls["n"] += 1
            return self._conn.execute(sql, *a, **k)

    class CountingCat:
        conn = None

    cat = CountingCat()
    cat.conn = CountingConn(catalogue.conn)
    hashes = _hashes_for(cat, ids)
    assert len(hashes) == 50
    assert calls["n"] == 1, f"{calls['n']} queries for 50 documents — should be batched"
