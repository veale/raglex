"""End-to-end checks against a real Postgres + pgvector instance.

Skipped unless RAGLEX_TEST_PG_URL points at a database (the CI/dev container).
Exercises every backend-divergent path: DDL, RETURNING ids, ON CONFLICT upserts,
pgvector vector_search, tsvector FTS, and the full hybrid SearchEngine — so the
Postgres spine (§7) is verified, not just written.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from raglex.core.models import (
    DocType,
    ExtractedVia,
    Record,
    RelationshipType,
    ResolutionStatus,
    TypedRelation,
)
from raglex.embeddings import EmbedStage, HashingEmbeddingProvider
from raglex.resolve import Resolver
from raglex.retrieval import SearchEngine
from raglex.storage import Catalogue, TextStore

PG_URL = os.environ.get("RAGLEX_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set RAGLEX_TEST_PG_URL to test Postgres")

_TABLES = [
    "embeddings", "document_tags", "rule_runs", "tag_rules", "document_assets",
    "pending_resolution", "citation_aliases", "relations", "sources", "documents",
]


@pytest.fixture
def pg(tmp_path):
    import psycopg

    with psycopg.connect(PG_URL) as conn:  # fresh schema each test
        conn.execute("DROP TABLE IF EXISTS " + ", ".join(_TABLES) + " CASCADE")
        conn.commit()
    cat = Catalogue(PG_URL)
    yield cat, TextStore(tmp_path / "text")
    cat.close()


def _rec(stable_id, text, **kw):
    rec = Record(
        source=kw.get("source", "eu-cellar"), stable_id=stable_id,
        ecli=stable_id if stable_id.startswith("ECLI") else None,
        doc_type=kw.get("doc_type", DocType.JUDGMENT), title=stable_id,
        court="CJEU", decision_date=date(2024, 1, 1), language="en", source_language="en",
        text=text, raw_bytes=(text or stable_id).encode(),
        relations=kw.get("relations", []), extracted_via=ExtractedVia.STRUCTURED,
    )
    rec.ensure_payload_hash()
    return rec


def test_pg_backend_is_postgres(pg):
    cat, _ = pg
    assert cat.backend == "postgres"


def test_document_relations_resolve_tagging(pg):
    cat, ts = pg
    cat.upsert_document(_rec("ECLI:EU:C:2020:1", "earlier authority"))
    rel = TypedRelation(
        relationship_type=RelationshipType.APPLIES, raw_citation_string="ECLI:EU:C:2020:1",
        dst_id="ECLI:EU:C:2020:1", resolution_status=ResolutionStatus.PENDING,
    )
    rec2 = _rec("ECLI:EU:C:2020:2", "applies it", relations=[rel])
    cat.upsert_document(rec2, text_path=str(ts.put(rec2.payload_hash, "applies it")))

    assert Resolver(cat).run().resolved == 1  # RETURNING-based ids + resolution
    edge = cat.relations_for("ECLI:EU:C:2020:2")[0]
    assert edge["resolution_status"] == "resolved" and edge["dst_id"] == "ECLI:EU:C:2020:1"

    # tagging engine (RETURNING add_rule, ON CONFLICT document_tags)
    from raglex.tagging import RuleEngine
    rid = RuleEngine(cat).add_rule("auth", {"predicate": "literal", "args": {"value": "applies"}})
    RuleEngine(cat).run_rule(rid)
    assert "auth" in cat.get_document("ECLI:EU:C:2020:2")["topic_tags"]


def test_pgvector_and_tsvector_hybrid_search(pg):
    cat, ts = pg
    for sid, txt in [
        ("ECLI:EU:C:2020:1", "The right to erasure of personal data under the GDPR."),
        ("ECLI:EU:C:2020:2", "Merger control and competition remedies before the Commission."),
        ("ECLI:EU:C:2020:3", "Personal data processing and the right of access by the data subject."),
    ]:
        rec = _rec(sid, txt)
        path = str(ts.put(rec.payload_hash, txt))
        cat.upsert_document(rec, text_path=path)

    # add_chunk → pgvector + tsvector; mark/clear via ON CONFLICT
    EmbedStage(cat, HashingEmbeddingProvider(dimensions=256), textstore=ts).run()

    # pgvector cosine search directly
    prov = HashingEmbeddingProvider(dimensions=256)
    qv = prov.embed(["right to erasure of personal data"])[0]
    vhits = cat.vector_search(qv, prov.name, prov.model, prov.model_version, limit=3)
    assert vhits and "score" in vhits[0]

    # tsvector FTS directly
    fts = cat.fts_chunks("erasure personal data", prov.name, prov.model, prov.model_version)
    assert any(h[0] in {"ECLI:EU:C:2020:1", "ECLI:EU:C:2020:3"} for h in fts)

    # full hybrid SearchEngine (RRF over pgvector + tsvector)
    hits = SearchEngine(cat, prov).search("right to erasure of personal data", k=3, expand_graph=False)
    assert hits and hits[0].doc_id != "ECLI:EU:C:2020:2"


def test_hnsw_index_created(pg):
    cat, ts = pg
    rec = _rec("a", "personal data erasure")
    cat.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, "personal data erasure")))
    EmbedStage(cat, HashingEmbeddingProvider(dimensions=128), textstore=ts).run()
    assert cat.create_vector_index(128) is True
    idx = cat.conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename='embeddings'"
    ).fetchall()
    assert any(r["indexname"] == "embeddings_hnsw_128" for r in idx)


def test_ops_aggregates_on_pg(pg):
    cat, _ = pg
    cat.upsert_document(_rec("a", "x", doc_type=DocType.JUDGMENT))
    cat.upsert_document(_rec("b", "y", doc_type=DocType.OPINION, source="uk-grc"))
    counts = cat.corpus_counts()
    assert counts["total"] == 2 and counts["by_doc_type"]["opinion"] == 1
    assert cat.queue_depths()["text_not_embedded"] == 2
