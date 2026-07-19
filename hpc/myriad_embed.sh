#!/bin/bash -l
# RagLex bulk-embed array job for UCL Myriad (SGE).
#
# Conceptual template — adjust NTASKS / wallclock / paths, then:
#     qsub -t 1-40 myriad_embed.sh
# The -t range MUST equal NTASKS below: task t processes shards t, t+N, t+2N …
# and each shard writes its result independently, so a task that dies at
# wallclock is simply resubmitted (finished shards are skipped).
#
# NOTE on modules/CUDA: deliberately NO `module load cuda/...` here. The venv's
# pip-installed PyTorch wheel bundles its own CUDA runtime; all the node needs
# is a recent-enough NVIDIA *driver*, which the GPU nodes have. The cluster
# docs' pinned module versions drift — don't depend on them.

# ---- resources -------------------------------------------------------------
#$ -l gpu=1
#$ -l h_rt=8:00:00
#$ -l mem=48G
#$ -l tmpfs=20G
#$ -N raglex-embed
#$ -cwd
# Prefer A100 nodes (bf16 fast path). Comment out to take any GPU (V100s work,
# ~2-3x slower):
##$ -ac allow=L

# ---- configuration ---------------------------------------------------------
NTASKS=40                                       # must match the qsub -t range
EXPORT_DIR="$HOME/Scratch/raglex-embed"          # where the shards were rsynced
VENV="$HOME/Scratch/raglex-venv"                 # created by myriad_setup.sh
export HF_HOME="$HOME/Scratch/hf-cache"          # model pre-downloaded here
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1   # compute nodes: no network needed
export TOKENIZERS_PARALLELISM=false

# ---- run -------------------------------------------------------------------
source "$VENV/bin/activate"
python "$EXPORT_DIR/embed_shards.py" \
    --dir "$EXPORT_DIR" \
    --task "${SGE_TASK_ID:-1}" \
    --stride "$NTASKS" \
    --batch-size 64
