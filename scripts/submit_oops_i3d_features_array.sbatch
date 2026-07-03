#!/usr/bin/env bash
#SBATCH --job-name=oops-i3d-arr
#SBATCH --account=faculty-acc
#SBATCH --qos=bgqos
#SBATCH --partition=faculty
#SBATCH --nodes=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --array=0-7
#SBATCH --output=logs/oops_i3d_array_%A_%a.out
#SBATCH --error=logs/oops_i3d_array_%A_%a.err

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/vast/users/guangyi.chen/causal_group/guangyi.chen}"
PYTHON_BIN="${PYTHON_BIN:-/vast/users/guangyi.chen/anaconda3/envs/simulation-hessian2-rocm/bin/python}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-datasets/oops/annotations}"
VIDEO_ROOT="${VIDEO_ROOT:-datasets/oops/raw/oops_video}"
FEATURES_DIR="${FEATURES_DIR:-datasets/oops/features/inception_i3d_fvd400_8fps16_stride4}"
BACKBONE="${BACKBONE:-inception_i3d_fvd400}"
SAMPLE_FPS="${SAMPLE_FPS:-8}"
CLIP_LEN="${CLIP_LEN:-16}"
STRIDE_FRAMES="${STRIDE_FRAMES:-4}"
BATCH_SIZE="${BATCH_SIZE:-16}"
NUM_SHARDS="${NUM_SHARDS:-8}"
SHARD_INDEX="${SLURM_ARRAY_TASK_ID:-${SHARD_INDEX:-0}}"

cd "${PROJECT_DIR}"
mkdir -p logs .cache/torch ".cache/miopen_array_${SHARD_INDEX}"

export TORCH_HOME="${TORCH_HOME:-${PROJECT_DIR}/.cache/torch}"
export MIOPEN_USER_DB_PATH="${PROJECT_DIR}/.cache/miopen_array_${SHARD_INDEX}"
export MIOPEN_CUSTOM_CACHE_DIR="${PROJECT_DIR}/.cache/miopen_array_${SHARD_INDEX}"

EXTRA_ARGS=()
if [[ -n "${CHECKPOINT_PATH:-}" ]]; then
  EXTRA_ARGS+=(--checkpoint-path "${CHECKPOINT_PATH}")
fi

"${PYTHON_BIN}" -m oops_temporal.extract_i3d_features \
  --annotations-dir "${ANNOTATIONS_DIR}" \
  --video-root "${VIDEO_ROOT}" \
  --features-dir "${FEATURES_DIR}" \
  --backbone "${BACKBONE}" \
  --sample-fps "${SAMPLE_FPS}" \
  --clip-len "${CLIP_LEN}" \
  --stride-frames "${STRIDE_FRAMES}" \
  --batch-size "${BATCH_SIZE}" \
  --num-shards "${NUM_SHARDS}" \
  --shard-index "${SHARD_INDEX}" \
  "${EXTRA_ARGS[@]}" \
  "$@"
