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
from raglex.storage.catalogue import _fts_parts


def test_fts_parts_stay_below_postgres_position_budget_even_when_text_is_short():
    # 25k tiny words fit beneath the old character cap but exceed PostgreSQL's
    # 16,383 stored positions, making phrases near a judgment's end unfindable.
    text = "word " * 25_000
    parts = _fts_parts(text, 400_000, word_cap=10_000)
    assert len(parts) == 3
    assert all(len(text[start:end].split()) <= 10_000 for start, end in parts)
    assert parts[0][0] == 0 and parts[-1][1] == len(text)


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


def test_fts_index_tolerates_converter_nul_without_changing_offsets(catalogue):
    text = "before\x00after"
    assert catalogue.put_doc_fts("nul-doc", text) == 1
    row = catalogue.conn.execute(
        "SELECT char_start, char_end FROM doc_fts WHERE doc_id = ?", ("nul-doc",)
    ).fetchone()
    assert (row["char_start"], row["char_end"]) == (0, len(text))


def test_fts_keeps_structural_headings_without_falsifying_body_offsets(catalogue):
    text = "1. The body never repeats its heading."
    catalogue.put_doc_fts(
        "headed-doc", text,
        headings=[("Article 51 Challenge of the competence of notified bodies", 0)])
    heading = catalogue.conn.execute(
        "SELECT label,char_start FROM doc_headings WHERE doc_id=?", ("headed-doc",)
    ).fetchone()
    assert (heading["label"], heading["char_start"]) == (
        "Article 51 Challenge of the competence of notified bodies", 0)
    body = catalogue.conn.execute(
        "SELECT char_start,char_end FROM doc_fts WHERE doc_id=?", ("headed-doc",)
    ).fetchone()
    assert (body["char_start"], body["char_end"]) == (0, len(text))


def test_heading_only_upgrade_preserves_an_existing_body_index(catalogue):
    catalogue.put_doc_fts("upgrade-doc", "authoritative body text")
    before = dict(catalogue.conn.execute(
        "SELECT part,char_start,char_end,words FROM doc_fts WHERE doc_id=?",
        ("upgrade-doc",)).fetchone())
    catalogue.put_doc_headings("upgrade-doc", [("Article 7 Scope", 3)])
    after = dict(catalogue.conn.execute(
        "SELECT part,char_start,char_end,words FROM doc_fts WHERE doc_id=?",
        ("upgrade-doc",)).fetchone())
    assert after == before
    assert "upgrade-doc" in catalogue.fts_body_indexed_ids()
    assert "upgrade-doc" in catalogue.fts_indexed_ids()


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


def test_harvest_delta_indexer_makes_new_judgment_searchable(catalogue, tmp_path):
    """A selected source's watch must extend doc_fts, not just the documents table."""
    from raglex.core.models import DocType, ExtractedVia, Record, Segment
    from raglex.facade import Facade
    from raglex.storage import TextStore

    ts = TextStore(tmp_path / "text")
    text = "1. The Data Protection Act 2018 applies to this Irish judgment."
    rec = Record(source="ie-caselaw", stable_id="iehc/2026/1",
                 doc_type=DocType.JUDGMENT, title="A v B", court="iehc",
                 text=text, raw_bytes=b"pdf", extracted_via=ExtractedVia.STRUCTURED,
                 segments=[Segment(label="[1]", char_start=0, char_end=len(text),
                                   kind="paragraph")])
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))
    ts.put_segments(rec.payload_hash, rec.segments)

    out = Facade._index_freetext_ids_open(catalogue, ts, [rec.stable_id])

    assert out["indexed"] == 1
    assert rec.stable_id in catalogue.fts_body_indexed_ids()
    assert catalogue.conn.execute(
        "SELECT label FROM doc_headings WHERE doc_id=?", (rec.stable_id,)
    ).fetchone()["label"] in ("A v B", "[1]")


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


# -- every passage, not just the first -----------------------------------------
def test_all_matching_passages_are_found():
    """A judgment that uses the phrase eight times is a different hit from one that
    uses it in passing; showing only the first passage hides that."""
    from raglex.fulltext.index import match_offsets

    p = parse('"duty of care"')
    text = ("a duty of care arose. " * 3) + "unrelated. " + "the duty of care again."
    offs = match_offsets(text, p)
    assert len(offs) == 4
    assert offs == sorted(offs)
    for o in offs:
        assert text[o:o + 12].lower() == "duty of care"


def test_passage_count_is_capped():
    from raglex.fulltext.index import match_offsets

    p = parse('"duty of care"')
    assert len(match_offsets("a duty of care. " * 200, p, cap=12)) == 12


def test_unquoted_queries_report_passages_from_their_first_term():
    from raglex.fulltext.index import match_offsets

    p = parse("negligence damages")
    text = "negligence here, damages there, and negligence again"
    assert len(match_offsets(text, p)) == 2


def test_a_document_matching_once_reports_one_passage():
    from raglex.fulltext.index import match_offsets

    assert len(match_offsets("only one duty of care here", parse('"duty of care"'))) == 1


# -- the agent-facing shape ----------------------------------------------------
def test_the_agent_response_omits_what_an_agent_cannot_use(tmp_path, monkeypatch):
    """The browser response carries compact metadata for every match — up to four
    thousand rows — because the page narrows locally. An agent pays for that in
    tokens and cannot act on it."""
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config(data_dir=tmp_path, catalogue_path=str(tmp_path / "c.sqlite"),
                      raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                      settings_path=tmp_path / "s.json",
                      embed_provider="local-hashing", embed_model=None))
    monkeypatch.setattr(f, "freetext_search", lambda *a, **k: {
        "items": [{"stable_id": "d1", "title": "A v B", "court": "ewca",
                   "court_label": "Court of Appeal", "jurisdiction": "United Kingdom",
                   "decision_date": "2015-01-01", "oscola": None}],
        "verified": 1, "total": 1, "truncated": False, "notes": [],
        "facets": {"jurisdiction": [{"value": "United Kingdom", "label": None, "n": 1}],
                   "doc_type": [], "court": [], "years": [{"year": "2015", "n": 1}]},
        "network": {"cites": [{"stable_id": "donoghue", "title": "Donoghue", "citing": 1}]},
        "matched": [{"id": "d1"}] * 4000,
    })
    monkeypatch.setattr(f, "freetext_hydrate", lambda **k: {"items": [
        {"stable_id": "d1", "cited_by": 12, "passage_count": 3,
         "passages": [{"anchor": "para 4", "snippet": "one"},
                      {"anchor": "para 9", "snippet": "two"},
                      {"anchor": "para 11", "snippet": "three"}]}]})

    out = f.freetext_for_agent("duty", passages=2)
    assert "matched" not in out, "the 4,000-row narrowing array must not reach an agent"
    assert out["total"] == 1 and out["shown"] == 1
    it = out["items"][0]
    assert it["passage_count"] == 3 and len(it["passages"]) == 2, "passages are capped"
    assert it["cited_by"] == 12
    # years roll to decades — an agent wants the shape, not 120 rows
    assert out["facets"]["decade"] == {"2010s": 1}
    assert out["commonly_cited"][0]["id"] == "donoghue"


def test_a_truncated_agent_result_says_the_total_is_a_lower_bound(tmp_path, monkeypatch):
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config(data_dir=tmp_path, catalogue_path=str(tmp_path / "c.sqlite"),
                      raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                      settings_path=tmp_path / "s.json",
                      embed_provider="local-hashing", embed_model=None))
    monkeypatch.setattr(f, "freetext_search", lambda *a, **k: {
        "items": [], "verified": 4000, "total": 9000, "truncated": True,
        "notes": [], "facets": {}, "network": {}, "matched": []})
    monkeypatch.setattr(f, "freetext_hydrate", lambda **k: {"items": []})
    out = f.freetext_for_agent("common words")
    assert "lower bound" in out["note"]


def test_an_unmatched_jurisdiction_filter_returns_nothing_not_everything(tmp_path):
    """``jurisdiction="eu"`` came back half full of UK assimilated instruments, each
    correctly labelled United Kingdom in its own facets — because the filter had not
    been applied at all.

    Two faults, one behaviour: the argument was compared against the display name
    ("European Union"), so the ISO code the tool documents matched no source; and the
    resulting EMPTY source list is falsy, so the filter was dropped and the search ran
    over the whole corpus. A filter that selects nothing must return nothing.
    """
    from raglex.config import Config
    from raglex.facade import Facade

    f = Facade(Config(data_dir=tmp_path, catalogue_path=str(tmp_path / "c.sqlite"),
                      raw_dir=tmp_path / "raw", text_dir=tmp_path / "text",
                      settings_path=tmp_path / "s.json",
                      embed_provider="local-hashing", embed_model=None))
    out = f.freetext_search("notified bodies", jurisdictions=["Kingdom of Erewhon"])
    assert out["items"] == [] and out["total"] == 0
    assert any("no indexed source lies in" in n for n in out["notes"])
    # …and the ISO code the tool advertises resolves, rather than matching nothing
    assert f._norm_jurisdiction("eu") == "European Union"
    assert f._norm_jurisdiction("gb") == "United Kingdom"
