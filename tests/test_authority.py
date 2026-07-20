"""Citation-network statistics (design §3): the PageRank authority roll-up,
its fusion into hybrid search, ranked graph expansion, co-citation/coupling
"related" queries, the citator, and the provision fetch."""

from __future__ import annotations

from datetime import date

from raglex.core.models import DocType, ExtractedVia, Record, RelationshipType, ResolutionStatus, TypedRelation
from raglex.embeddings import EmbedStage, HashingEmbeddingProvider
from raglex.retrieval import SearchEngine, expand
from raglex.retrieval.authority import compute_authority, decay_weight
from raglex.storage import TextStore


# -- pure math ---------------------------------------------------------------
def test_pagerank_favours_the_cited():
    rows = {r[0]: r for r in compute_authority(
        [("b", "a"), ("c", "a"), ("d", "a"), ("a", "e")], {}, now_year=2026)}
    # cited documents (a, e) outrank the uncited citing leaves (b, c, d)
    assert rows["a"][1] > rows["b"][1]
    assert rows["e"][1] > rows["b"][1]
    assert rows["a"][4] == 3 and rows["a"][5] == 1  # in/out degree
    # percentile only among cited docs; uncited citers get None
    assert rows["b"][3] is None and rows["a"][3] is not None


def test_decay_prefers_recent_citers():
    # x is cited by an old case, y by a recent one — raw ties, decayed differs
    rows = {r[0]: r for r in compute_authority(
        [("old", "x"), ("new", "y")], {"old": 1985, "new": 2025}, now_year=2026)}
    assert abs(rows["x"][1] - rows["y"][1]) < 1e-9      # raw pagerank ties
    assert rows["y"][2] > rows["x"][2]                  # decayed favours the recent citation


def test_decay_weight_halves_per_half_life():
    w10 = decay_weight(2016, now_year=2026, half_life=10)
    assert abs(w10 - 0.5) < 1e-9
    assert decay_weight(None, now_year=2026) < decay_weight(2026, now_year=2026)


def test_pg_ddl_survives_the_semicolon_splitter():
    """The Postgres shim's executescript splits PG_DDL on ';' WITHOUT stripping
    comments — a semicolon inside a comment shears the script mid-sentence and
    takes the whole API down at startup (it did, 2026-07). Guard the invariant."""
    import re

    from raglex.storage import _postgres

    for line in _postgres.PG_DDL.splitlines():
        assert not (line.strip().startswith("--") and ";" in line), line
    for frag in _postgres.PG_DDL.split(";"):
        body = re.sub(r"--[^\n]*", "", frag).strip()
        assert not body or re.match(r"(?i)^(CREATE|ALTER|DROP|INSERT|SET)\b", body), body[:80]


# -- catalogue round-trip ----------------------------------------------------
def _doc(catalogue, ts, sid, text="text", dt=DocType.JUDGMENT, when=date(2024, 1, 1),
         relations=None):
    rec = Record(source="t", stable_id=sid, doc_type=dt, title=f"Doc {sid}", court="ct",
                 decision_date=when, language="en", text=text, raw_bytes=text.encode(),
                 relations=relations or [], extracted_via=ExtractedVia.STRUCTURED)
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


def _edge(catalogue, src, dst, *, via="regex"):
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, resolution_status, relationship_type, extracted_via) "
        "VALUES (?,?,'resolved','mentions',?)", (src, dst, via))
    catalogue.conn.commit()


def _mini_graph(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    for sid, when in [("A", date(2010, 1, 1)), ("B", date(2020, 1, 1)),
                      ("C", date(2024, 1, 1)), ("D", date(2023, 1, 1))]:
        _doc(catalogue, ts, sid, f"body of {sid}", when=when)
    _edge(catalogue, "B", "A")
    _edge(catalogue, "C", "A")
    _edge(catalogue, "C", "B")
    _edge(catalogue, "D", "A")
    _edge(catalogue, "D", "B")
    return ts


def test_rebuild_authority_and_lookup(catalogue, tmp_path):
    _mini_graph(catalogue, tmp_path)
    n = catalogue.rebuild_authority()
    assert n == 4
    auth = catalogue.authority_for(["A", "B", "C"])
    assert auth["A"]["pagerank"] > auth["B"]["pagerank"] > 0
    assert auth["A"]["in_degree"] == 3
    assert auth["A"]["percentile"] == 100.0
    # inferred edges are excluded from the ranking graph
    _edge(catalogue, "C", "D", via="inferred")
    catalogue.rebuild_authority()
    assert "D" not in catalogue.authority_for(["D"]) or \
        catalogue.authority_for(["D"])["D"]["in_degree"] == 0


def test_expand_ranks_by_authority(catalogue, tmp_path):
    _mini_graph(catalogue, tmp_path)
    catalogue.rebuild_authority()
    # from C, neighbour A (authority) should come before B (less cited)
    exp = expand(catalogue, "C", limit=2)
    assert [n.dst_id for n in exp.neighbours][0] == "A"
    assert exp.neighbours[0].authority > 0


def test_related_co_citation_and_coupling(catalogue, tmp_path):
    _mini_graph(catalogue, tmp_path)
    # A and B are cited together by C and D → co-cited; C and D share authorities → coupled
    co = catalogue.co_cited_with(["A"])
    assert co and co[0]["id"] == "B" and co[0]["n"] == 2
    coupled = catalogue.coupled_with("C")
    assert any(r["id"] == "D" and r["n"] == 2 for r in coupled)


def test_cited_by_stats_and_top_citors(catalogue, tmp_path):
    _mini_graph(catalogue, tmp_path)
    catalogue.rebuild_authority()
    stats = catalogue.cited_by_stats(["A"])
    assert stats["documents"] == 3
    assert stats["recent_documents"] >= 2  # C (2024) and D (2023)
    tops = catalogue.top_citors(["A"], limit=2)
    assert len(tops) == 2  # ranked by the citers' own pagerank


# -- fusion + signals --------------------------------------------------------
def test_search_signals_and_authority_fusion(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _doc(catalogue, ts, "X1", "the right to erasure of personal data")
    _doc(catalogue, ts, "X2", "the right to erasure of personal data indeed")
    # X2 is heavily cited; X1 not at all
    for citer in ("C1", "C2", "C3"):
        _doc(catalogue, ts, citer, f"citing body {citer}")
        _edge(catalogue, citer, "X2")
    catalogue.rebuild_authority()
    EmbedStage(catalogue, HashingEmbeddingProvider(dimensions=512)).run()
    engine = SearchEngine(catalogue, HashingEmbeddingProvider(dimensions=512))
    hits = engine.search("right to erasure of personal data", k=4, expand_graph=False)
    by_id = {h.doc_id: h for h in hits}
    assert "X2" in by_id
    sig = by_id["X2"].signals
    assert sig["authority_rank"] == 1
    assert sig["semantic_rank"] is not None or sig["lexical_rank"] is not None
    # the uncited doc carries no authority signal
    if "X1" in by_id:
        assert by_id["X1"].signals["authority_rank"] is None


# -- facade: citator / related / provision / bulk decisions ------------------
def _facade(tmp_path):
    from raglex.config import Config
    from raglex.facade import Facade

    cfg = Config(
        data_dir=tmp_path, catalogue_path=tmp_path / "cat.sqlite", raw_dir=tmp_path / "raw",
        text_dir=tmp_path / "text", settings_path=tmp_path / "settings.json",
        embed_provider="local-hashing", embed_model=None)
    return Facade(cfg)


def test_facade_citator_and_related(tmp_path):
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _mini_graph_facade(cat, ts)
        cat.rebuild_authority()
    c = f.citator("A")
    assert c["cited_by"]["documents"] == 3
    assert c["authority"]["percentile"] == 100.0
    assert c["treatments"] is None  # deliberately absent until reliable
    rel = f.related_documents("A")
    assert rel["co_cited"] and rel["co_cited"][0]["id"] == "B"
    assert rel["co_cited"][0]["title"] == "Doc B"


def _mini_graph_facade(cat, ts):
    for sid, when in [("A", date(2010, 1, 1)), ("B", date(2020, 1, 1)),
                      ("C", date(2024, 1, 1)), ("D", date(2023, 1, 1))]:
        _doc(cat, ts, sid, f"body of {sid}", when=when)
    _edge(cat, "B", "A")
    _edge(cat, "C", "A")
    _edge(cat, "C", "B")
    _edge(cat, "D", "A")
    _edge(cat, "D", "B")


def test_facade_get_provision(tmp_path):
    f = _facade(tmp_path)
    text = "Article 1 Scope\nThis law applies broadly.\n\nArticle 2 Definitions\nTerms mean things."
    from raglex.core.models import Segment
    rec = Record(source="t", stable_id="law/1", doc_type=DocType.LEGISLATION, title="A Law",
                 language="en", text=text, raw_bytes=text.encode(),
                 segments=[
                     Segment(label="Article 1", kind="article", level=0, char_start=0, char_end=39),
                     Segment(label="Article 2", kind="article", level=0, char_start=41, char_end=len(text)),
                 ])
    rec.ensure_payload_hash()
    with f._open() as (cat, _rs, ts):
        path = str(ts.put(rec.payload_hash, text))
        ts.put_segments(rec.payload_hash, rec.segments)
        cat.upsert_document(rec, text_path=path)
    out = f.get_provision("law/1", label="Article 2", context=1)
    assert not out.get("error")
    focus = [s for s in out["segments"] if s["focus"]]
    assert focus and "Definitions" in focus[0]["text"]
    # context brought the neighbour along
    assert any(s["label"] == "Article 1" for s in out["segments"])
    # char-span entry (the search-hit path)
    out2 = f.get_provision("law/1", char_start=45, context=0)
    assert out2["segments"][0]["label"] == "Article 2"


def test_facade_decide_suggestions_bulk(tmp_path):
    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        _doc(cat, ts, "target/1", "the target")
        cat.put_suggestion("Some Ref v X", "target/1", kind="report", reason="name match")
        cat.put_suggestion("Other Ref v Y", "target/1", kind="report", reason="name match")
    r = f.decide_suggestions(items=[
        {"ref": "Some Ref v X", "suggested_id": "target/1", "accept": True},
        {"ref": "Other Ref v Y", "suggested_id": "target/1", "accept": False},
    ])
    assert r["decided"] == 2 and r["accepted"] == 1 and not r["errors"]
    with f._open() as (cat, _rs, _ts):
        assert cat.count_pending_suggestions() == 0


def test_reference_context_snippets(tmp_path):
    f = _facade(tmp_path)
    body = "As was held in Foo v Bar [1999] 1 WLR 1, the principle applies strictly."
    with f._open() as (cat, _rs, ts):
        _doc(cat, ts, "citing/1", body)
        cat.conn.execute(
            "INSERT INTO relations (src_id, dst_id, raw_citation_string, raw_fold, "
            "resolution_status, relationship_type, extracted_via, context_start, context_end) "
            "VALUES ('citing/1', NULL, ?, ?, 'pending', 'mentions', 'regex', 15, 40)",
            ("Foo v Bar [1999] 1 WLR 1", "foo v bar [1999] 1 wlr 1"))
        cat.conn.commit()
    out = f.reference_context("Foo v Bar [1999] 1 WLR 1")
    assert out["occurrences"] and "principle applies" in out["occurrences"][0]["snippet"]


def test_corpus_shape_and_drill(tmp_path):
    from datetime import date as _d

    f = _facade(tmp_path)
    with f._open() as (cat, _rs, ts):
        specs = [("uksc/2020/1", "uk-caselaw", "judgment", _d(2020, 1, 1)),
                 ("ukpga/2018/12", "uk-legislation", "legislation", _d(2018, 5, 23)),
                 ("ECLI:EU:C:2020:559", "eu-cellar", "judgment", _d(2020, 7, 16))]
        from raglex.core.models import DocType, Record
        for sid, src, dt, when in specs:
            r = Record(source=src, stable_id=sid, doc_type=DocType(dt), title="T " + sid,
                       court="ct", decision_date=when, language="en",
                       text="x " * 40, raw_bytes=sid.encode())
            r.ensure_payload_hash()
            cat.upsert_document(r, text_path=str(ts.put(r.payload_hash, r.text)))
        _edge(cat, "uksc/2020/1", "ukpga/2018/12")
        cat.rebuild_authority()
    shape = f._corpus_shape_uncached()
    juris = {j["jurisdiction"]: j for j in shape["jurisdictions"]}
    assert juris["United Kingdom"]["total"] == 2
    assert juris["United Kingdom"]["cases"] == 1
    assert juris["United Kingdom"]["legislation"] == 1
    assert juris["European Union"]["total"] == 1
    assert shape["total"] == 3
    # top authority present with an OSCOLA rendering
    assert juris["United Kingdom"]["top_authority"][0]["id"] == "ukpga/2018/12"

    drill = f.jurisdiction_drill("United Kingdom", kind="legislation")
    assert drill["items"][0]["id"] == "ukpga/2018/12"
    # hanging groupings: what cites the act, by citing doc type
    assert drill["items"][0]["hanging"] == {"judgment": 1}
    # kind filter really filters
    assert all(i["doc_type"] in ("judgment", "decision", "opinion")
               for i in f.jurisdiction_drill("United Kingdom", kind="cases")["items"])


def _cited_by_edge(catalogue, seed, citer):
    # the CELLAR forward-discovery scaffold: stored REVERSED (src=cited seed,
    # dst=citer) because the citer isn't held yet — see facade.find_citing
    catalogue.conn.execute(
        "INSERT INTO relations (src_id, dst_id, resolution_status, relationship_type, extracted_via) "
        "VALUES (?,?,'resolved','cited_by','structured')", (seed, citer))
    catalogue.conn.commit()


def test_cited_by_scaffold_edges_never_read_as_forward_citations(catalogue, tmp_path):
    # A (2010) is cited by C (2024). The forward fact is a normal edge C->A. The
    # CELLAR discovery scaffold ALSO records it reversed as A --cited_by--> C.
    # That reversed edge must not make C look cited-by-A (backwards in time), nor
    # inflate C's authority — the exact C-5/77 / C-359/92 defect.
    ts = TextStore(tmp_path / "text")
    for sid, when in [("A", date(2010, 1, 1)), ("C", date(2024, 1, 1))]:
        _doc(catalogue, ts, sid, f"body {sid}", when=when)
    _edge(catalogue, "C", "A")             # real, forward-in-time
    _cited_by_edge(catalogue, "A", "C")    # reverse-oriented scaffold

    # C's cited-by panel must be EMPTY — A does not cite C
    assert catalogue.cited_by_stats(["C"])["documents"] == 0
    assert catalogue.top_citors(["C"]) == []
    assert [r["src_id"] for r in catalogue.top_citing_edges(["C"])] == []
    assert [r["src_id"] for r in catalogue.relations_to("C")] == []
    # A's cited-by is unaffected (the real C->A edge still counts)
    assert catalogue.cited_by_stats(["A"])["documents"] == 1

    # and PageRank isn't fed the backwards edge: with only A<-C, A outranks C
    catalogue.rebuild_authority()
    a = catalogue.authority_for(["A"])["A"]["pagerank"]
    c = catalogue.authority_for(["C"])["C"]["pagerank"]
    assert a > c


def test_cited_by_counts_batches_many_ids_into_one_aggregate(catalogue, tmp_path):
    """The cited-by panel annotates each citer with its own citation count. Asking per
    row would be one query per row on a page showing 200 — the N+1 that pinned a pool
    connection per view — so the counts come back for the whole page at once."""
    ts = TextStore(tmp_path / "text")
    for sid in ("A", "B", "C", "D"):
        _doc(catalogue, ts, sid, f"body of {sid}")
    _edge(catalogue, "C", "A")
    _edge(catalogue, "D", "A")
    _edge(catalogue, "D", "B")

    counts = catalogue.cited_by_counts(["A", "B", "C"])
    assert counts["A"] == 2          # cited by C and D
    assert counts["B"] == 1          # cited by D
    assert "C" not in counts         # nothing cites C — absent, not zero-padded


def test_cited_by_counts_ignores_inferred_and_self_edges(catalogue, tmp_path):
    """Same exclusions as the headline cited-by total, or the subtle "[cited by N]"
    cue would contradict the number printed at the top of the panel."""
    ts = TextStore(tmp_path / "text")
    for sid in ("A", "B"):
        _doc(catalogue, ts, sid, f"body of {sid}")
    _edge(catalogue, "B", "A")
    _edge(catalogue, "B", "A", via="inferred")   # heuristic carry-forward
    _edge(catalogue, "A", "A")                   # self-citation
    assert catalogue.cited_by_counts(["A"])["A"] == 1
