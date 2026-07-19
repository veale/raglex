"""Offline (HPC) embedding — the file-relay pipeline (design §1b / §8 stage 1).

The database lives on a small home server; the GPUs live on a university cluster
(UCL Myriad) that cannot reach the database. So the bulk embed pass is a relay of
*files*, resumable at every step, with the chunking done HERE (where the corpus
and its structure live) and only pure model inference done on the cluster:

1. ``raglex embed-export``  → chunk every pending document with the standard
   structure-aware chunker and write **chunk shards** (``shard-00001.jsonl.gz``)
   plus a ``manifest.json`` stamping the embedding *family* the vectors will
   belong to. A document's chunks never straddle two shards, so every shard is
   independently importable.
2. rsync the export directory to the cluster; run ``hpc/embed_shards.py`` as an
   SGE array job (one task per shard, one GPU each) — writes
   ``shard-00001.vec.bin`` (raw little-endian float16, row-major) +
   ``shard-00001.vec.json`` (ids + dims + model echo) next to each input shard.
3. rsync back; ``raglex embed-import`` validates every vector shard against the
   manifest (family, dims, id alignment), L2-normalises, writes chunks + FTS
   rows, and marks documents embedded. Already-imported shards are skipped, so
   the import is re-runnable and can trail the cluster job.

The vector format is deliberately numpy-free on the RagLex side: float16 pairs
parse with stdlib ``struct`` ('e'), so the API image needs no scientific stack.

Family discipline (§6d): the manifest stamps ``("hf", model, revision, dims)`` —
named after the *weights*, not the transport — and the query-time provider
(``tei``/``hf`` in provider.py) must be configured to the same triple, so vectors
computed on an A100 and queries embedded by the serving container land in ONE
comparable family.
"""

from __future__ import annotations

import gzip
import json
import logging
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..storage.catalogue import Catalogue
from ..storage.textstore import TextStore
from .chunking import ChunkConfig, chunk_document, doc_proxy_chunk
from .embed import EmbedStage, _doc_meta
from .tei import query_prefix

log = logging.getLogger("raglex.embeddings.offline")

FORMAT_VERSION = 1


@dataclass(slots=True)
class ExportStats:
    documents: int = 0
    chunks: int = 0
    shards: int = 0
    skipped_no_text: int = 0


def export_shards(
    catalogue: Catalogue,
    textstore: TextStore | None,
    out_dir: str | Path,
    *,
    model: str,
    model_version: str = "1",
    dimensions: int,
    provider_label: str = "hf",
    chunks_per_shard: int = 25_000,
    limit: int | None = None,
    chunk_config: ChunkConfig | None = None,
    include_doc_text: bool = True,
    doc_vector: bool = True,
    on_progress=None,
) -> ExportStats:
    """Chunk every document pending in the target family into JSONL shards.

    Chunking runs here so the cluster job is pure inference — the chunker needs
    the segment sidecars and document metadata, which only this side has.

    ``include_doc_text`` writes one ``{"kind": "doc_text", …}`` record per
    document ahead of its chunks. The default worker ignores them; the
    late-chunking worker mode needs them (whole-document token context). They're
    written by default because the shards are the expensive shipped artifact —
    a re-export to add late chunking later would cost more than the extra bytes
    (gzip amortises the near-duplication heavily).

    ``doc_vector`` adds the document-level summary-proxy chunk (chunk_id = -1,
    design §2.2), embedded like any other chunk on the cluster."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = chunk_config or ChunkConfig()
    stats = ExportStats()

    pending = catalogue.pending_embedding(provider_label, model, model_version)
    if limit is not None:
        pending = pending[:limit]
    total = len(pending)

    shard_rows: list[dict] = []
    shard_docs = 0
    shard_meta: list[dict] = []

    def _flush():
        nonlocal shard_rows, shard_docs
        if not shard_rows:
            return
        stats.shards += 1
        name = f"shard-{stats.shards:05d}.jsonl.gz"
        with gzip.open(out / name, "wt", encoding="utf-8") as fh:
            for row in shard_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        # "chunks" counts embeddable rows only — doc_text records ride along for
        # the late-chunking worker mode but are never embedded or imported
        n_chunks = sum(1 for r in shard_rows if "kind" not in r)
        shard_meta.append({"name": name, "chunks": n_chunks, "documents": shard_docs})
        shard_rows = []
        shard_docs = 0

    for i, row in enumerate(pending, 1):
        if on_progress and (i % 500 == 0 or i == total):
            on_progress(stage="exporting", done=i, total=total, item=row["stable_id"])
        text = EmbedStage._load_text(row["text_path"])
        if not text:
            stats.skipped_no_text += 1
            continue
        segments = (
            textstore.get_segments(row["payload_hash"])
            if textstore and row["payload_hash"] else []
        )
        chunks = chunk_document(row["stable_id"], text, segments=segments,
                                meta=_doc_meta(row), config=cfg)
        if not chunks:
            stats.skipped_no_text += 1
            continue
        if doc_vector:
            proxy = doc_proxy_chunk(row["stable_id"], text, meta=_doc_meta(row))
            if proxy is not None:
                chunks.append(proxy)
        # keep a document's chunks in ONE shard so each shard imports independently
        if shard_rows and len(shard_rows) + len(chunks) + 1 > chunks_per_shard:
            _flush()
        if include_doc_text:
            shard_rows.append({"kind": "doc_text", "doc_id": row["stable_id"], "text": text})
        for c in chunks:
            shard_rows.append({
                "doc_id": c.doc_id, "chunk_id": c.chunk_id,
                "text": c.text, "embed_input": c.embed_input,
                "structural_unit": c.structural_unit,
                "char_start": c.char_start, "char_end": c.char_end,
                "source_language": row["source_language"],
            })
        shard_docs += 1
        stats.documents += 1
        stats.chunks += len(chunks)
    _flush()

    manifest = {
        "format_version": FORMAT_VERSION,
        "family": {"provider": provider_label, "model": model,
                   "model_version": model_version, "dimensions": dimensions},
        # recorded for reproducibility: what a QUERY gets prefixed with at serve
        # time (documents are embedded bare — same convention as EmbedStage)
        "query_instruction_prefix": query_prefix(model),
        "chunk_config": {"min_tokens": cfg.min_tokens, "target_tokens": cfg.target_tokens,
                         "max_tokens": cfg.max_tokens, "overlap_tokens": cfg.overlap_tokens},
        "includes_doc_text": include_doc_text,
        "includes_doc_vector": doc_vector,
        "documents": stats.documents, "chunks": stats.chunks,
        "shards": shard_meta,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("exported %d docs / %d chunks into %d shards at %s",
             stats.documents, stats.chunks, stats.shards, out)
    return stats


@dataclass(slots=True)
class ImportStats:
    shards_imported: int = 0
    shards_skipped: int = 0
    shards_missing_vectors: int = 0
    documents: int = 0
    chunks: int = 0


def _read_vec_shard(bin_path: Path, meta_path: Path) -> tuple[list[list[float]], dict]:
    """Parse a raw-fp16 vector shard + its JSON sidecar without numpy."""
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    n, dims = meta["count"], meta["dimensions"]
    buf = bin_path.read_bytes()
    expect = n * dims * 2
    if len(buf) != expect:
        raise ValueError(f"{bin_path.name}: {len(buf)} bytes, expected {expect} "
                         f"({n} × {dims} × fp16)")
    flat = struct.unpack(f"<{n * dims}e", buf)
    return [list(flat[i * dims:(i + 1) * dims]) for i in range(n)], meta


def _l2(vec: list[float]) -> list[float]:
    s = sum(x * x for x in vec) ** 0.5
    return [x / s for x in vec] if s > 0 else vec


def import_shards(
    catalogue: Catalogue,
    in_dir: str | Path,
    *,
    on_progress=None,
    cancel_check=None,
) -> ImportStats:
    """Import every vector shard present next to its chunk shard. Validates the
    family + id alignment, L2-normalises, writes vectors + FTS, marks documents
    embedded. Re-runnable: a shard whose documents are already embedded in this
    family is skipped, so the import can run repeatedly while the cluster job is
    still producing shards."""
    src = Path(in_dir)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    fam = manifest["family"]
    stats = ImportStats()
    shards = manifest["shards"]

    for si, sh in enumerate(shards, 1):
        if cancel_check and cancel_check():
            break
        chunk_path = src / sh["name"]
        base = sh["name"].removesuffix(".jsonl.gz")
        bin_path = src / f"{base}.vec.bin"
        meta_path = src / f"{base}.vec.json"
        if not bin_path.exists() or not meta_path.exists():
            stats.shards_missing_vectors += 1
            continue
        vectors, vmeta = _read_vec_shard(bin_path, meta_path)
        if vmeta.get("model") and vmeta["model"] != fam["model"]:
            raise ValueError(f"{base}: embedded with {vmeta['model']!r} but manifest "
                             f"family is {fam['model']!r} — refusing to mix families")
        dims = vmeta["dimensions"]
        if dims != fam["dimensions"]:
            raise ValueError(
                f"{base}: vectors are {dims}-dim but the manifest family is "
                f"{fam['dimensions']}-dim. If you truncated (Matryoshka) on the cluster, "
                f"re-export with --dimensions {dims} so the family is stamped correctly.")

        rows = []
        with gzip.open(chunk_path, "rt", encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if "kind" in row:  # doc_text records: worker context only, never embedded
                    continue
                rows.append(row)
        if len(rows) != len(vectors):
            raise ValueError(f"{base}: {len(rows)} chunks but {len(vectors)} vectors")
        if vmeta.get("ids"):
            for row, (d, c) in zip(rows, vmeta["ids"]):
                if row["doc_id"] != d or row["chunk_id"] != c:
                    raise ValueError(f"{base}: id misalignment at ({d}, {c})")

        # skip shards whose docs are all already imported in this family (re-run)
        doc_ids = list(dict.fromkeys(r["doc_id"] for r in rows))
        existing = catalogue.embedded_docs_in_family(
            doc_ids, fam["provider"], fam["model"], fam["model_version"])
        if len(existing) == len(doc_ids):
            stats.shards_skipped += 1
            continue

        if on_progress:
            on_progress(stage="importing", done=si, total=len(shards), item=sh["name"])
        by_doc: dict[str, list[tuple[dict, list[float]]]] = {}
        for row, vec in zip(rows, vectors):
            by_doc.setdefault(row["doc_id"], []).append((row, vec))
        for doc_id, items in by_doc.items():
            catalogue.clear_embeddings(doc_id, fam["provider"], fam["model"], fam["model_version"])
            for row, vec in items:
                catalogue.add_chunk(
                    doc_id, row["chunk_id"], _l2(vec), row["text"],
                    provider=fam["provider"], model=fam["model"],
                    model_version=fam["model_version"], dimensions=dims,
                    structural_unit=row["structural_unit"],
                    source_language=row.get("source_language"),
                    char_start=row["char_start"], char_end=row["char_end"],
                )
                stats.chunks += 1
            catalogue.mark_embedded(doc_id)
            stats.documents += 1
        catalogue.conn.commit()
        stats.shards_imported += 1
    return stats
