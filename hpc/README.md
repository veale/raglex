# Bulk embedding on UCL Myriad (or any SGE cluster)

The database lives on asahi; the GPUs live at UCL. Neither can talk to the
other, so the bulk embed pass is a **file relay** — resumable at every step,
with chunking done where the corpus lives and *only model inference* on the
cluster:

```
asahi/laptop                     Myriad                          asahi
raglex embed-export ── rsync ──▶ qsub array job ── rsync back ──▶ raglex embed-import
(chunks + manifest)              (embed_shards.py                (validate, normalise,
                                  writes .vec.bin per shard)      write pgvector + FTS)
```

The **family invariant** holds it together: the export manifest stamps
`("hf", model, revision, dims)`; the import refuses vectors that disagree; and
the query-time provider (`RAGLEX_EMBED_PROVIDER=tei`, serving the same model)
uses the identical family — so cluster-computed vectors and live queries meet
in one comparable index.

## Sizing (measured against the live corpus, 2026-07)

| Quantity | Value |
|---|---|
| Documents with text | ~909k (of 1.04M) |
| Extracted text | ~30 GB (avg ~34 KB/doc) |
| Estimated chunks (350-token target) | ~20–25 M |
| Estimated tokens (incl. contextual headers) | ~8 B |
| Export size (gzipped shards) | ~15–20 GB (~900 shards; includes per-doc full text + doc-level proxy rows) |
| Vectors back (fp16, 1024-d) | ~45 GB |
| **GPU time, Qwen3-Embedding-0.6B, A100** | **~30–60 GPU-hours** |
| — same on V100 (`gpu=1` without `-ac allow=L`) | ~2–3× that |
| — Qwen3-Embedding-4B instead | ~4–6× that (150–250 A100-h) |
| — BGE-M3 (dense) | similar to 0.6B |

As a 40-task array (`-t 1-40`, one GPU each, ~22 shards/task) each task runs
**~1–2 h on an A100** — comfortably inside an 8 h wallclock, and short tasks
schedule fast. With ~10 tasks running concurrently the whole pass is an
afternoon; even a single GPU does it in ~2 days. Start with **0.6B**, run
`raglex bench`, and only pay for 4B if the numbers say so.

**Disk on asahi after import** (the cost nobody budgets): pgvector stores
fp32, so 22M × 1024-d ≈ **90 GB** of vectors + ~30 GB chunk text + tens of GB
of HNSW index. If that's too rich, export/embed at `--dimensions 512`
(Matryoshka truncation — valid for Qwen3, NOT for BGE-M3) and halve it; the
LIMIT-paper caveat is that lower dims tighten the representational ceiling, so
don't go below 512 for the primary family.

## Step by step

### 0. One-time cluster setup (login node — it has internet)

```bash
scp hpc/myriad_setup.sh hpc/myriad_embed.sh hpc/embed_shards.py myriad.rc.ucl.ac.uk:~/
ssh myriad.rc.ucl.ac.uk bash myriad_setup.sh Qwen/Qwen3-Embedding-0.6B
```

Creates `~/Scratch/raglex-venv` (pip torch = bundled CUDA runtime — no
`module load cuda`, and don't trust the docs' pinned module versions) and
pre-downloads the model into `~/Scratch/hf-cache` so compute nodes never need
the network (`HF_HUB_OFFLINE=1` in the jobscript).

### 1. Export (wherever the DB is reachable — asahi itself, or via the API container)

```bash
# pilot first — a few hundred docs end-to-end before committing GPU hours:
raglex embed-export --out /data/embed-export --model Qwen/Qwen3-Embedding-0.6B \
    --dimensions 1024 --limit 500
# then the real thing (an hour-ish of chunking; resumable by re-running):
raglex embed-export --out /data/embed-export --model Qwen/Qwen3-Embedding-0.6B \
    --dimensions 1024
```

### 2. Ship to Scratch

```bash
rsync -avP /data/embed-export/ myriad.rc.ucl.ac.uk:~/Scratch/raglex-embed/
scp hpc/embed_shards.py myriad.rc.ucl.ac.uk:~/Scratch/raglex-embed/
```

### 3. Run the array job

```bash
ssh myriad.rc.ucl.ac.uk
qsub -t 1-40 myriad_embed.sh     # -t range must equal NTASKS in the script
qstat                            # watch; tasks that hit wallclock just get resubmitted
```

Each task claims shards `t, t+40, t+80, …`, skips any shard that already has a
`.vec.json`, and writes results shard-by-shard — **resubmitting the same
command resumes exactly where it stopped**, including after a partial rsync
back.

### 4. Fetch results + import

```bash
rsync -avP --include='*.vec.*' --exclude='*' \
    myriad.rc.ucl.ac.uk:~/Scratch/raglex-embed/ /data/embed-export/
raglex embed-import --dir /data/embed-export
# re-runnable: skips what's in, reports how many shards still await vectors —
# you can import in waves while the cluster is still crunching
raglex index        # build the HNSW index once all shards are in
```

### 5. Turn on serving + verify

```bash
# docker-compose.asahi.yml has a 'tei' service (profile: ml) serving the same
# model for query traffic (CPU is fine for queries; ~ms per query at 0.6B):
docker compose --profile ml up -d raglex-tei
# then configure (Settings page or env):
#   RAGLEX_EMBED_PROVIDER=tei
#   RAGLEX_EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B
#   RAGLEX_EMBED_DIMENSIONS=1024
#   RAGLEX_RERANKER=tei            # if also serving bge-reranker-v2-m3
raglex bench --queries 300        # known-item eval; reports land in data/bench/
```

`raglex bench` is how families get compared: run it once per candidate
(swap the RAGLEX_EMBED_* settings between runs) and diff the JSON — promotion
to production default should be a number, not a vibe.

## Chunking modes

The export carries three kinds of row per document (all on by default):

- **leaf chunks** — the structural units, each with a contextual header
  (`[UK · court · year · title · Part › Chapter · unit]`) in the embed input;
- **a doc-level proxy** (`chunk_id = -1`) — title + opening + tail, embedded
  like any chunk; answers "which case is about this" while retrieval's
  containment rule stops it duplicating its own leaves in results;
- **`doc_text` records** — the full document text, *never embedded*, shipped so
  that **late chunking is a worker flag, not a re-export**:

```bash
# a second family with whole-document context (BGE-M3 etc. — mean-pooled models
# only; the worker refuses Qwen3-Embedding, whose last-token pooling makes
# span-pooling unsound):
python embed_shards.py --dir ~/Scratch/raglex-embed --model BAAI/bge-m3 \
    --late-chunking --task "$SGE_TASK_ID" --stride 40
```

Late chunking costs ~2–4× the plain pass (windowed full-document forward
passes) — budget ~100–200 A100-hours for the corpus, and let `raglex bench`
decide whether the recall gain on legislation/defined-terms queries earns it.

## Notes

- **Queue etiquette**: 40 single-GPU tasks schedule far better than one 4-GPU
  job, and lost work on preemption/wallclock is one shard, not the run.
- **A second family** (e.g. BGE-M3 for A/B) is the same relay with a different
  `--model`: families coexist in the DB by design; nothing is overwritten.
- **Instruction prefix**: queries to instruction-tuned models get a legal-task
  prefix at serve time (`RAGLEX_EMBED_INSTRUCTION` to override); documents are
  embedded bare. The manifest records the prefix for reproducibility.
- **Don't** run the export against SQLite dev data and import into production —
  the import validates the family but cannot know the chunks came from a
  different corpus. One export dir per target database.
