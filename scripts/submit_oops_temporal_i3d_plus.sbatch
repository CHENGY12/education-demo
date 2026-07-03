#!/usr/bin/env bash
#SBATCH --job-name=oops-plus
#SBATCH --account=faculty-acc
#SBATCH --qos=bgqos
#SBATCH --partition=faculty
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=80G
#SBATCH --time=06:00:00
#SBATCH --output=logs/oops_temporal_plus_%j.out
#SBATCH --error=logs/oops_temporal_plus_%j.err

set -euo pipefail

cd /vast/users/guangyi.chen/causal_group/guangyi.chen

PYTHON_BIN="${PYTHON_BIN:-/vast/users/guangyi.chen/anaconda3/envs/simulation-hessian2-rocm/bin/python}"
FEATURES_DIR="${FEATURES_DIR:-datasets/oops/features/inception_i3d_fvd400_8fps16_stride4}"
RUN_NAME="${RUN_NAME:-i3d_fvd400_temporal_plus_${SLURM_JOB_ID:-local}}"
OUTPUT_DIR="${OUTPUT_DIR:-runs/oops_temporal/${RUN_NAME}}"

export PYTHONNOUSERSITE=1
export PYTHONPATH="${PWD}:${PYTHONPATH:-}"
export TORCH_HOME="${TORCH_HOME:-${PWD}/.cache/torch}"
export MIOPEN_USER_DB_PATH="${MIOPEN_USER_DB_PATH:-${PWD}/.cache/miopen_user_db_temporal_plus}"
export MIOPEN_CUSTOM_CACHE_DIR="${MIOPEN_CUSTOM_CACHE_DIR:-${PWD}/.cache/miopen_custom_cache_temporal_plus}"

mkdir -p logs runs/oops_temporal "${TORCH_HOME}" "${MIOPEN_USER_DB_PATH}" "${MIOPEN_CUSTOM_CACHE_DIR}"

echo "[train] output_dir=${OUTPUT_DIR}"
echo "[train] features_dir=${FEATURES_DIR}"

"${PYTHON_BIN}" -m oops_temporal.train \
  --annotations-dir datasets/oops/annotations \
  --feature-mode features \
  --features-dir "${FEATURES_DIR}" \
  --skip-missing-features \
  --append-time-features \
  --model transformer \
  --num-bins "${NUM_BINS:-128}" \
  --label-sigma-bins "${LABEL_SIGMA_BINS:-1.5}" \
  --hidden-dim "${HIDDEN_DIM:-256}" \
  --num-layers "${NUM_LAYERS:-4}" \
  --num-heads "${NUM_HEADS:-4}" \
  --dropout "${DROPOUT:-0.2}" \
  --batch-size "${BATCH_SIZE:-128}" \
  --epochs "${EPOCHS:-50}" \
  --lr "${LR:-2e-4}" \
  --weight-decay "${WEIGHT_DECAY:-0.02}" \
  --warmup-ratio "${WARMUP_RATIO:-0.10}" \
  --hard-ce-weight "${HARD_CE_WEIGHT:-0.5}" \
  --regression-loss-weight "${REGRESSION_LOSS_WEIGHT:-0.3}" \
  --regression-temperature "${REGRESSION_TEMPERATURE:-1.5}" \
  --metric-prediction "${METRIC_PREDICTION:-blend}" \
  --metric-temperature "${METRIC_TEMPERATURE:-1.0}" \
  --metric-argmax-weight "${METRIC_ARGMAX_WEIGHT:-0.75}" \
  --metric-local-window "${METRIC_LOCAL_WINDOW:-16}" \
  --save-best-metric "${SAVE_BEST_METRIC:-acc_010}" \
  --save-best-mode "${SAVE_BEST_MODE:-max}" \
  --num-workers "${NUM_WORKERS:-4}" \
  --amp \
  --output-dir "${OUTPUT_DIR}"

echo "[done] training complete"
