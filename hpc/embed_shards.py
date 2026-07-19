#!/usr/bin/env python3
"""Standalone GPU embedding worker for RagLex chunk shards.

Runs on a cluster node with NO RagLex install and NO database access — its whole
contract is files: read ``shard-NNNNN.jsonl.gz`` (produced by ``raglex
embed-export``), write ``shard-NNNNN.vec.bin`` (raw little-endian float16,
row-major) + ``shard-NNNNN.vec.json`` (ids/dims/model echo) next to it.

Dependencies: torch + sentence-transformers (+ numpy, which torch brings).
Everything else is stdlib. Designed for SGE array jobs:

    # task t of an N-task array processes shards t, t+N, t+2N, …
    python embed_shards.py --dir ~/Scratch/raglex-embed \
        --task "$SGE_TASK_ID" --stride "$NTASKS"

Resumable at shard granularity: a shard whose ``.vec.json`` already exists is
skipped, so a job that hits wallclock just gets resubmitted.

Matryoshka truncation: ``--dims 512`` slices each vector to the first 512
components before writing (valid for MRL-trained models like Qwen3-Embedding;
do NOT truncate BGE-M3). The RagLex import refuses shards whose dims disagree
with the export manifest, so pass the same --dimensions to ``raglex
embed-export`` as you pass --dims here.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path


def load_model(name: str, *, trust_remote_code: bool = False):
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    kwargs = {"device": device, "trust_remote_code": trust_remote_code}
    # bf16 halves memory and is faster on A100s; harmless on CPU fallback
    if device == "cuda":
        kwargs["model_kwargs"] = {"torch_dtype": torch.bfloat16}
    model = SentenceTransformer(name, **kwargs)
    return model, device


def _load_shard(shard_path: Path) -> tuple[list[dict], dict[str, str]]:
    """Split a shard into embeddable chunk rows and the per-doc full texts
    (``kind: doc_text`` records — present when the export was future-proofed
    for late chunking; ignored in normal mode)."""
    rows: list[dict] = []
    doc_texts: dict[str, str] = {}
    with gzip.open(shard_path, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("kind") == "doc_text":
                doc_texts[row["doc_id"]] = row["text"]
            else:
                rows.append(row)
    return rows, doc_texts


def _truncate_norm(vecs, dims: int | None):
    import numpy as np

    if dims and vecs.shape[1] > dims:
        vecs = vecs[:, :dims]
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def encode_late(model, rows: list[dict], doc_texts: dict[str, str], *,
                window_tokens: int = 7936, stride_tokens: int = 1024):
    """Late chunking (design §1d): run each WHOLE document through the encoder,
    then mean-pool token embeddings per chunk char-span — so a mid-judgment
    holding's vector is contextualised by the Act named forty paragraphs earlier.

    ONLY sound for bidirectional mean-pooled encoders (BGE-M3, jina, e5): span
    mean-pooling matches how those models were trained. Qwen3-Embedding pools on
    the last token of a causal stack — spans there are off-distribution, so this
    script refuses (use normal mode for Qwen3).

    Documents longer than the window are processed in overlapping windows and
    each chunk pools from the window whose centre is nearest — long-range
    context degrades gracefully rather than truncating.

    The per-doc contextual-header prefix (shared across the doc's chunks) is
    prepended once to the document text, so header signal is kept. Chunks whose
    doc_text is missing (or the doc-level -1 proxy, which has no true span) fall
    back to independent encoding."""
    import numpy as np
    import torch

    tok = model.tokenizer
    encoder = model[0].auto_model
    device = next(encoder.parameters()).device

    by_doc: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        by_doc.setdefault(r["doc_id"], []).append(i)

    out = np.zeros((len(rows), encoder.config.hidden_size), dtype=np.float32)
    fallback_idx: list[int] = []

    for doc_id, idxs in by_doc.items():
        text = doc_texts.get(doc_id)
        spanful = [i for i in idxs
                   if text and rows[i]["chunk_id"] >= 0 and rows[i]["char_start"] is not None]
        fallback_idx.extend(i for i in idxs if i not in spanful)
        if not spanful:
            continue
        # header context once, spans shifted accordingly
        r0 = rows[spanful[0]]
        header = r0["embed_input"][: len(r0["embed_input"]) - len(r0["text"])]
        full = header + text
        shift = len(header)

        enc = tok(full, return_offsets_mapping=True, return_tensors="pt",
                  truncation=False, add_special_tokens=True)
        offsets = enc.pop("offset_mapping")[0].tolist()
        ids = enc["input_ids"][0]
        n_tok = ids.shape[0]

        # window starts so every token is covered; overlap = window - stride
        starts = list(range(0, max(1, n_tok - window_tokens + 1), window_tokens - stride_tokens))
        if starts[-1] + window_tokens < n_tok:
            starts.append(n_tok - window_tokens)
        hidden = torch.zeros(n_tok, encoder.config.hidden_size)
        counted = torch.zeros(n_tok, 1)
        with torch.no_grad():
            for s in starts:
                e = min(n_tok, s + window_tokens)
                window = {k: v[:, s:e].to(device) for k, v in enc.items()}
                h = encoder(**window).last_hidden_state[0].float().cpu()
                hidden[s:e] += h
                counted[s:e] += 1
        hidden = hidden / counted.clamp(min=1)

        for i in spanful:
            a = rows[i]["char_start"] + shift
            b = rows[i]["char_end"] + shift
            token_ix = [t for t, (o1, o2) in enumerate(offsets) if o2 > a and o1 < b]
            if not token_ix:
                fallback_idx.append(i)
                continue
            out[i] = hidden[token_ix].mean(dim=0).numpy()

    if fallback_idx:
        fb = model.encode([rows[i]["embed_input"] for i in fallback_idx],
                          show_progress_bar=False, convert_to_numpy=True,
                          normalize_embeddings=False)
        for j, i in enumerate(fallback_idx):
            out[i] = fb[j]
    return out


def process_shard(model, shard_path: Path, *, batch_size: int, dims: int | None,
                  model_name: str, late: bool = False) -> dict:
    rows, doc_texts = _load_shard(shard_path)

    t0 = time.time()
    if late:
        vecs = encode_late(model, rows, doc_texts)
    else:
        vecs = model.encode(
            [r["embed_input"] for r in rows], batch_size=batch_size,
            show_progress_bar=False, convert_to_numpy=True, normalize_embeddings=False,
        )
    vecs = _truncate_norm(vecs, dims)
    took = time.time() - t0

    base = shard_path.name.removesuffix(".jsonl.gz")
    out_bin = shard_path.parent / f"{base}.vec.bin"
    out_meta = shard_path.parent / f"{base}.vec.json"
    vecs.astype("<f2").tofile(out_bin)
    out_meta.write_text(json.dumps({
        "count": int(vecs.shape[0]),
        "dimensions": int(vecs.shape[1]),
        "model": model_name,
        "dtype": "float16-le",
        "ids": [[r["doc_id"], r["chunk_id"]] for r in rows],
        "seconds": round(took, 1),
    }))
    return {"chunks": len(rows), "seconds": took}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True, help="the export directory (shards + manifest.json)")
    ap.add_argument("--model", default=None,
                    help="HF model id; default = the export manifest's family model")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--dims", type=int, default=None,
                    help="Matryoshka-truncate vectors to this width (must match the manifest)")
    ap.add_argument("--task", type=int, default=1, help="1-based array task id (SGE_TASK_ID)")
    ap.add_argument("--stride", type=int, default=1, help="total array tasks")
    ap.add_argument("--late-chunking", action="store_true",
                    help="pool per-chunk from whole-document token embeddings (needs doc_text "
                         "records in the export; bidirectional mean-pool models ONLY, e.g. "
                         "BGE-M3 — refused for last-token-pooled models like Qwen3-Embedding)")
    ap.add_argument("--trust-remote-code", action="store_true")
    args = ap.parse_args()

    root = Path(args.dir).expanduser()
    manifest = json.loads((root / "manifest.json").read_text())
    model_name = args.model or manifest["family"]["model"]
    dims = args.dims or manifest["family"]["dimensions"]
    if args.late_chunking and "qwen3-embedding" in model_name.lower():
        ap.error("--late-chunking is unsound for last-token-pooled Qwen3-Embedding; "
                 "use it with mean-pooled models (BGE-M3, jina, e5)")

    shards = [root / s["name"] for s in manifest["shards"]]
    mine = shards[args.task - 1::args.stride]
    todo = [s for s in mine
            if not (s.parent / (s.name.removesuffix(".jsonl.gz") + ".vec.json")).exists()]
    print(f"[task {args.task}/{args.stride}] {len(mine)} shards assigned, "
          f"{len(todo)} to do, model={model_name} dims={dims}", flush=True)
    if not todo:
        return 0

    model, device = load_model(model_name, trust_remote_code=args.trust_remote_code)
    print(f"model loaded on {device}", flush=True)
    done_chunks = 0
    t0 = time.time()
    for i, shard in enumerate(todo, 1):
        r = process_shard(model, shard, batch_size=args.batch_size, dims=dims,
                          model_name=model_name, late=args.late_chunking)
        done_chunks += r["chunks"]
        rate = done_chunks / max(1e-9, time.time() - t0)
        print(f"  [{i}/{len(todo)}] {shard.name}: {r['chunks']} chunks in "
              f"{r['seconds']:.0f}s  (cumulative {rate:.0f} chunks/s)", flush=True)
    print(f"[task {args.task}] done: {done_chunks} chunks in "
          f"{(time.time() - t0) / 3600:.2f}h", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
