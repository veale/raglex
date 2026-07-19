#!/bin/bash -l
# One-time setup on a Myriad LOGIN node (login nodes have internet; compute
# nodes are treated as offline). Creates a venv on Scratch and pre-downloads
# the embedding model into an HF cache the jobscript points at.
#
#     bash myriad_setup.sh [MODEL_ID]
#
# No `module load cuda` — the pip torch wheel carries its own CUDA runtime.
# If the default python3 is old (<3.10), `module avail python` and load a
# recent one first; the exact version is not critical.
set -euo pipefail

MODEL="${1:-Qwen/Qwen3-Embedding-0.6B}"
VENV="$HOME/Scratch/raglex-venv"
export HF_HOME="$HOME/Scratch/hf-cache"

echo "== venv at $VENV"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip
# torch pulls the CUDA-enabled wheel by default on linux x86_64
pip install torch sentence-transformers "huggingface_hub[cli]"

echo "== pre-downloading $MODEL into $HF_HOME"
hf download "$MODEL"

echo "== smoke test (CPU on the login node — slow but proves the stack)"
python - <<PY
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("$MODEL")
v = m.encode(["s 45 Data Protection Act 2018 right of access"], normalize_embeddings=True)
print("ok:", v.shape)
PY

echo "== done. rsync your export dir to ~/Scratch/raglex-embed and qsub myriad_embed.sh"
