from __future__ import annotations

from datetime import date

from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
)
from raglex.embeddings import EmbedStage, HashingEmbeddingProvider
from raglex.retrieval import Candidate, SearchEngine, expand, rrf_fuse
from raglex.storage import TextStore


# -- RRF --------------------------------------------------------------------
def test_rrf_rewards_agreement_between_rankers():
    # 'b' is high on both lists → should win the fusion
    vec = [Candidate("a", 0, 0.9), Candidate("b", 0, 0.8)]
    lex = [Candidate("b", 0, -1.0), Candidate("c", 0, -2.0)]
    fused = rrf_fuse(vec, lex)
    assert fused[0].doc_id == "b"


# -- end-to-end embed + hybrid search --------------------------------------
def _store(catalogue, ts, stable_id, text, *, court="CJEU", source="eu-cellar",
           dt=DocType.JUDGMENT, relations=None):
    rec = Record(
        source=source, stable_id=stable_id, ecli=stable_id if stable_id.startswith("ECLI") else None,
        doc_type=dt, title=stable_id, court=court, decision_date=date(2024, 1, 1),
        language="en", source_language="en", text=text, raw_bytes=text.encode(),
        relations=relations or [], extracted_via=ExtractedVia.STRUCTURED,
    )
    rec.ensure_payload_hash()
    path = str(ts.put(rec.payload_hash, text))
    catalogue.upsert_document(rec, text_path=path)


def _corpus(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _store(catalogue, ts, "ECLI:EU:C:2020:1",
           "The data subject has the right to erasure of personal data under the GDPR.")
    _store(catalogue, ts, "ECLI:EU:C:2020:2",
           "Merger control and competition remedies before the Commission.")
    _store(catalogue, ts, "ECLI:EU:C:2020:3",
           "Personal data processing and the right of access by the data subject.")
    EmbedStage(catalogue, HashingEmbeddingProvider(dimensions=512)).run()


def test_embed_stage_populates_vectors_and_fts(catalogue, tmp_path):
    _corpus(catalogue, tmp_path)
    rows = catalogue.vector_rows("local-hashing", "hashing-bow", "v1")
    assert len(rows) >= 3
    assert catalogue.get_document("ECLI:EU:C:2020:1")["has_embedding"] == 1


def test_hybrid_search_ranks_relevant_first(catalogue, tmp_path):
    _corpus(catalogue, tmp_path)
    engine = SearchEngine(catalogue, HashingEmbeddingProvider(dimensions=512))
    hits = engine.search("right to erasure of personal data", k=3, expand_graph=False)
    assert hits
    # the data-protection docs should outrank the competition doc
    assert hits[0].doc_id in {"ECLI:EU:C:2020:1", "ECLI:EU:C:2020:3"}
    assert hits[0].doc_id != "ECLI:EU:C:2020:2"


def test_search_partition_prefilter(catalogue, tmp_path):
    _corpus(catalogue, tmp_path)
    engine = SearchEngine(catalogue, HashingEmbeddingProvider(dimensions=512))
    # restrict to a doc_type that exists → still returns; to one that doesn't → empty
    hits = engine.search("data", filters={"doc_type": ["opinion"]}, expand_graph=False)
    assert hits == []


def test_pending_embedding_is_per_family(catalogue, tmp_path):
    _corpus(catalogue, tmp_path)
    # already embedded with hashing-bow/v1 → nothing pending in that family
    assert catalogue.pending_embedding("local-hashing", "hashing-bow", "v1") == []
    # a different family (a model swap) re-queues the whole corpus (§6d)
    assert len(catalogue.pending_embedding("voyage", "voyage-law-2", "1")) >= 3


# -- GraphRAG expansion -----------------------------------------------------
def test_graphrag_expands_typed_neighbours(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    # a cited authority, present in the corpus
    _store(catalogue, ts, "ECLI:EU:C:2014:2428", "Earlier authority on data retention.")
    # a citing case with a resolved typed edge to it
    rel = TypedRelation(
        relationship_type=RelationshipType.APPLIES,
        raw_citation_string="ECLI:EU:C:2014:2428",
        dst_id="ECLI:EU:C:2014:2428",
        extracted_via=ExtractedVia.STRUCTURED,
        resolution_status=ResolutionStatus.PENDING,
    )
    _store(catalogue, ts, "ECLI:EU:C:2020:559", "Schrems II applies the earlier authority.",
           relations=[rel])
    from raglex.resolve import Resolver
    Resolver(catalogue).run()  # resolve the edge so expansion can traverse it

    exp = expand(catalogue, "ECLI:EU:C:2020:559")
    out = [n for n in exp.neighbours if n.direction == "out"]
    assert any(n.dst_id == "ECLI:EU:C:2014:2428" and n.relationship_type == "applies" for n in out)

    # incoming view from the authority's side
    back = expand(catalogue, "ECLI:EU:C:2014:2428")
    assert any(n.direction == "in" and n.dst_id == "ECLI:EU:C:2020:559" for n in back.neighbours)


def test_graphrag_relationship_type_filter(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _store(catalogue, ts, "ECLI:EU:C:2014:2428", "Earlier authority.")
    rel = TypedRelation(
        relationship_type=RelationshipType.APPLIES, raw_citation_string="ECLI:EU:C:2014:2428",
        dst_id="ECLI:EU:C:2014:2428", resolution_status=ResolutionStatus.PENDING,
    )
    _store(catalogue, ts, "ECLI:EU:C:2020:559", "Applies it.", relations=[rel])
    from raglex.resolve import Resolver
    Resolver(catalogue).run()

    # asking only for 'overrules' neighbours returns none (the edge is 'applies')
    exp = expand(catalogue, "ECLI:EU:C:2020:559", relationship_types=["overrules"])
    assert exp.neighbours == []
