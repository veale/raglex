"""The flexible-embeddings slice (design §1b/§2.1/§5): ancestor-path headers,
the offline HPC shard relay (export → fake GPU worker → import), the TEI
provider's family/instruction behaviour, and the bench harness."""

from __future__ import annotations

import gzip
import json
import struct
from datetime import date
from pathlib import Path

from raglex.core.models import DocType, ExtractedVia, Record, Segment
from raglex.embeddings.chunking import ChunkConfig, chunk_document
from raglex.embeddings.offline import export_shards, import_shards
from raglex.embeddings.tei import query_prefix, wants_instruction
from raglex.storage import TextStore


# -- contextual headers (design §2.1) ---------------------------------------
def test_header_carries_title_and_ancestor_path():
    text = "Part 2 Rights\nChapter 1 Access\nSection 45\nThe data subject may request access."
    segments = [
        Segment(label="Part 2", kind="part", level=0, char_start=0, char_end=13),
        Segment(label="Chapter 1", kind="chapter", level=1, char_start=14, char_end=30),
        Segment(label="s. 45", kind="section", level=2, char_start=31, char_end=len(text)),
    ]
    chunks = chunk_document("law/x", text, segments=segments,
                            meta={"jurisdiction": "UK", "year": "2018",
                                  "title": "Data Protection Act 2018"},
                            config=ChunkConfig(min_tokens=1))
    leaf = [c for c in chunks if c.structural_unit == "s. 45"]
    assert leaf, [c.structural_unit for c in chunks]
    head = leaf[0].embed_input
    assert "Data Protection Act 2018" in head
    assert "Part 2 › Chapter 1" in head
    # the display text stays clean — header is embedding-input only
    assert "Part 2 ›" not in leaf[0].text


def test_header_sibling_sections_do_not_nest():
    segments = [
        Segment(label="Article 1", kind="article", level=0, char_start=0, char_end=10),
        Segment(label="Article 2", kind="article", level=0, char_start=11, char_end=21),
    ]
    text = "0123456789 0123456789"
    chunks = chunk_document("law/y", text, segments=segments, meta=None,
                            config=ChunkConfig(min_tokens=1))
    # Article 2's path must not contain its SIBLING Article 1
    a2 = [c for c in chunks if c.structural_unit == "Article 2"]
    if a2:  # tiny units may merge; when they do the merged unit keeps the first label
        assert "Article 1 ›" not in a2[0].embed_input


# -- TEI provider conventions ------------------------------------------------
def test_instruction_prefix_only_for_instruction_models():
    assert wants_instruction("Qwen/Qwen3-Embedding-0.6B")
    assert not wants_instruction("BAAI/bge-m3")
    assert query_prefix("Qwen/Qwen3-Embedding-0.6B").startswith("Instruct: ")
    assert query_prefix("BAAI/bge-m3") == ""
    assert "retrieve the statutory provisions" in query_prefix("Qwen/Qwen3-Embedding-4B")


def test_tei_family_is_canonical_hf():
    from raglex.embeddings import get_provider

    p = get_provider("tei", model="Qwen/Qwen3-Embedding-0.6B", dimensions=1024)
    assert p.name == "hf"  # named after the weights, not the transport
    p2 = get_provider("hf", model="Qwen/Qwen3-Embedding-0.6B", dimensions=1024)
    assert p.family == p2.family


# -- the offline relay: export → (fake GPU) → import -------------------------
def _seed_corpus(catalogue, ts: TextStore, n: int = 5):
    for i in range(n):
        text = (f"Judgment {i}. The right to erasure of personal data is engaged. "
                "The court considered the statutory scheme in detail. " * 6)
        rec = Record(source="t", stable_id=f"case/{i}", doc_type=DocType.JUDGMENT,
                     title=f"Case {i}", court="ct", decision_date=date(2024, 1, 1),
                     language="en", source_language="en", text=text,
                     raw_bytes=text.encode(), extracted_via=ExtractedVia.STRUCTURED)
        rec.ensure_payload_hash()
        catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, text)))


def _fake_gpu_worker(export_dir: Path, dims: int = 8):
    """Stand-in for hpc/embed_shards.py: same outputs, deterministic vectors.
    Skips doc_text records exactly as the real worker's _load_shard does."""
    manifest = json.loads((export_dir / "manifest.json").read_text())
    for sh in manifest["shards"]:
        with gzip.open(export_dir / sh["name"], "rt", encoding="utf-8") as fh:
            rows = [r for r in (json.loads(line) for line in fh) if "kind" not in r]
        vecs = []
        for r in rows:
            h = hash(r["embed_input"]) & 0xFFFF
            vecs.append([((h >> b) & 1) * 1.0 + 0.1 for b in range(dims)])
        base = sh["name"].removesuffix(".jsonl.gz")
        flat = [x for v in vecs for x in v]
        (export_dir / f"{base}.vec.bin").write_bytes(struct.pack(f"<{len(flat)}e", *flat))
        (export_dir / f"{base}.vec.json").write_text(json.dumps({
            "count": len(vecs), "dimensions": dims,
            "model": manifest["family"]["model"], "dtype": "float16-le",
            "ids": [[r["doc_id"], r["chunk_id"]] for r in rows],
        }))


def test_export_worker_import_roundtrip(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _seed_corpus(catalogue, ts, n=5)
    out = tmp_path / "export"

    stats = export_shards(catalogue, ts, out, model="test/model", model_version="r1",
                          dimensions=8, chunks_per_shard=4)
    assert stats.documents == 5 and stats.shards >= 2
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["family"] == {"provider": "hf", "model": "test/model",
                                  "model_version": "r1", "dimensions": 8}
    assert manifest["includes_doc_text"] and manifest["includes_doc_vector"]

    # each doc ships its full text (late-chunking future-proofing) + a -1 doc vector
    first = manifest["shards"][0]["name"]
    with gzip.open(out / first, "rt", encoding="utf-8") as fh:
        rows0 = [json.loads(line) for line in fh]
    kinds = [r.get("kind") for r in rows0]
    assert "doc_text" in kinds
    assert any(r.get("chunk_id") == -1 and r.get("structural_unit") == "doc"
               for r in rows0 if "kind" not in r)
    # manifest chunk counts exclude the doc_text records
    assert manifest["shards"][0]["chunks"] == len([r for r in rows0 if "kind" not in r])

    # import before any vectors exist → all shards reported as awaiting
    st0 = import_shards(catalogue, out)
    assert st0.shards_imported == 0 and st0.shards_missing_vectors == stats.shards

    _fake_gpu_worker(out, dims=8)
    st1 = import_shards(catalogue, out)
    assert st1.shards_imported == stats.shards
    assert st1.documents == 5 and st1.chunks == stats.chunks

    # vectors landed in the family, L2-normalised, and docs are marked embedded
    rows = catalogue.vector_rows("hf", "test/model", "r1")
    assert len(rows) == stats.chunks
    assert catalogue.get_document("case/0")["has_embedding"] == 1
    assert catalogue.pending_embedding("hf", "test/model", "r1") == []

    # re-running the import is a no-op (skip-what's-done)
    st2 = import_shards(catalogue, out)
    assert st2.shards_imported == 0 and st2.shards_skipped == stats.shards


def test_import_refuses_wrong_dims(catalogue, tmp_path):
    ts = TextStore(tmp_path / "text")
    _seed_corpus(catalogue, ts, n=2)
    out = tmp_path / "export"
    export_shards(catalogue, ts, out, model="test/model", dimensions=8, chunks_per_shard=100)
    _fake_gpu_worker(out, dims=4)  # cluster truncated but manifest says 8
    import pytest

    with pytest.raises(ValueError, match="re-export with --dimensions 4"):
        import_shards(catalogue, out)


def test_imported_vectors_are_searchable(catalogue, tmp_path):
    """End-to-end: the relay's vectors serve hybrid search through a provider
    with the same family (here: a stub provider that mimics the fake worker)."""
    from raglex.retrieval import SearchEngine

    ts = TextStore(tmp_path / "text")
    _seed_corpus(catalogue, ts, n=3)
    out = tmp_path / "export"
    export_shards(catalogue, ts, out, model="test/model", model_version="r1",
                  dimensions=8, chunks_per_shard=100)
    _fake_gpu_worker(out, dims=8)
    import_shards(catalogue, out)

    class StubProvider:
        name, model, model_version, dimensions = "hf", "test/model", "r1", 8
        max_input_tokens = 8192

        def embed(self, texts, *, input_type=None):
            return [[0.5] * 8 for _ in texts]

        def health(self):
            return True

        @property
        def family(self):
            return (self.name, self.model, self.model_version, self.dimensions)

    hits = SearchEngine(catalogue, StubProvider()).search(
        "right to erasure of personal data", k=3, expand_graph=False)
    assert hits and hits[0].doc_id.startswith("case/")


def test_containment_drops_doc_row_when_leaf_hits():
    from raglex.retrieval.hybrid import Candidate
    from raglex.retrieval.search import _contain

    cands = [
        Candidate("A", 3, 1.0),    # leaf hit for A
        Candidate("A", -1, 0.9),   # A's doc-level row → dropped (leaf present)
        Candidate("B", -1, 0.8),   # B appears ONLY at doc level → kept
    ]
    kept = _contain(cands)
    assert ("A", -1) not in [(c.doc_id, c.chunk_id) for c in kept]
    assert ("B", -1) in [(c.doc_id, c.chunk_id) for c in kept]
    assert ("A", 3) in [(c.doc_id, c.chunk_id) for c in kept]


def test_embed_stage_emits_doc_vector(catalogue, tmp_path):
    from raglex.embeddings import EmbedStage, HashingEmbeddingProvider

    ts = TextStore(tmp_path / "text")
    _seed_corpus(catalogue, ts, n=1)
    EmbedStage(catalogue, HashingEmbeddingProvider(dimensions=64), textstore=ts).run()
    rows = catalogue.vector_rows("local-hashing", "hashing-bow", "v1")
    units = {r["chunk_id"]: r["structural_unit"] for r in rows}
    assert units.get(-1) == "doc"
    assert any(cid >= 0 for cid in units)


# -- bench harness -----------------------------------------------------------
def test_bench_known_item(catalogue, tmp_path):
    from raglex.embeddings import EmbedStage, HashingEmbeddingProvider
    from raglex.retrieval.bench import run_bench

    ts = TextStore(tmp_path / "text")
    # target authority + citing docs whose citation context names its subject
    target_text = "The landmark ruling on the right to erasure of personal data. " * 10
    rec = Record(source="t", stable_id="ECLI:EU:C:2014:317", doc_type=DocType.JUDGMENT,
                 title="Google Spain", court="CJEU", decision_date=date(2014, 5, 13),
                 language="en", text=target_text, raw_bytes=target_text.encode())
    rec.ensure_payload_hash()
    catalogue.upsert_document(rec, text_path=str(ts.put(rec.payload_hash, target_text)))
    for i in range(4):
        cite = "ECLI:EU:C:2014:317"
        body = ("Considering the delisting request and the right to erasure of personal "
                f"data from search results, this tribunal follows {cite} on whether the "
                "operator of a search engine must remove links to lawfully published pages. "
                "The balance of interests favours the data subject in case number %d." % i)
        r = Record(source="t", stable_id=f"citing/{i}", doc_type=DocType.JUDGMENT,
                   title=f"Citing {i}", court="ct", decision_date=date(2023, 1, 1),
                   language="en", text=body, raw_bytes=body.encode())
        r.ensure_payload_hash()
        catalogue.upsert_document(r, text_path=str(ts.put(r.payload_hash, body)))
        start = body.index(cite)
        catalogue.conn.execute(
            "INSERT INTO citations (src_id, raw, entity_kind, candidate_id, char_start, "
            "char_end, method, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"citing/{i}", cite, "case", cite, start, start + len(cite), "regex",
             "2026-01-01"))
    catalogue.conn.commit()

    provider = HashingEmbeddingProvider(dimensions=512)
    EmbedStage(catalogue, provider).run()
    report = run_bench(catalogue, ts, provider, queries=4, k=5, seed=1)
    assert report["queries"] > 0
    assert report["recall_at_k"] is not None and report["recall_at_k"] > 0
    assert report["family"].startswith("local-hashing/")
