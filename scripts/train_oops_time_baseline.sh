#!/usr/bin/env bash
set -euo pipefail

python -m oops_temporal.build_index \
  --annotations-dir datasets/oops/annotations \
  --output datasets/oops/oops_index.csv

python -m oops_temporal.train \
  --annotations-dir datasets/oops/annotations \
  --feature-mode time \
  --model mlp \
  --num-bins 64 \
  --hidden-dim 128 \
  --batch-size 512 \
  --epochs 10 \
  --lr 3e-4 \
  --weight-decay 0.05 \
  --num-workers 0 \
  --output-dir runs/oops_temporal/time_mlp_baseline
