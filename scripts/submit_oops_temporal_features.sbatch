#!/usr/bin/env bash
#SBATCH --job-name=oops-temp
#SBATCH --partition=faculty
#SBATCH --nodes=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=logs/oops_temporal_%j.out
#SBATCH --error=logs/oops_temporal_%j.err

set -euo pipefail

mkdir -p logs

python -m oops_temporal.train \
  --annotations-dir datasets/oops/annotations \
  --feature-mode features \
  --features-dir "${FEATURES_DIR:-datasets/oops/features}" \
  --model transformer \
  --num-bins "${NUM_BINS:-128}" \
  --hidden-dim "${HIDDEN_DIM:-256}" \
  --num-layers "${NUM_LAYERS:-4}" \
  --num-heads "${NUM_HEADS:-4}" \
  --batch-size "${BATCH_SIZE:-128}" \
  --epochs "${EPOCHS:-30}" \
  --lr "${LR:-3e-4}" \
  --weight-decay "${WEIGHT_DECAY:-0.05}" \
  --num-workers "${NUM_WORKERS:-8}" \
  --amp \
  --output-dir "${OUTPUT_DIR:-runs/oops_temporal/feature_transformer}"

