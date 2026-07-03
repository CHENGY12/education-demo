#!/usr/bin/env bash
#SBATCH --job-name=oops-i3d
#SBATCH --partition=faculty
#SBATCH --nodes=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=logs/oops_i3d_%j.out
#SBATCH --error=logs/oops_i3d_%j.err

set -euo pipefail

mkdir -p logs

PYTHON_BIN="${PYTHON_BIN:-/vast/users/guangyi.chen/anaconda3/envs/simulation-hessian2-rocm/bin/python}"
export TORCH_HOME="${TORCH_HOME:-/vast/users/guangyi.chen/causal_group/guangyi.chen/.cache/torch}"
export MIOPEN_USER_DB_PATH="${MIOPEN_USER_DB_PATH:-/vast/users/guangyi.chen/causal_group/guangyi.chen/.cache/miopen}"
export MIOPEN_CUSTOM_CACHE_DIR="${MIOPEN_CUSTOM_CACHE_DIR:-/vast/users/guangyi.chen/causal_group/guangyi.chen/.cache/miopen}"
mkdir -p "${MIOPEN_USER_DB_PATH}" "${MIOPEN_CUSTOM_CACHE_DIR}"

EXTRA_ARGS=()
if [[ -n "${CHECKPOINT_PATH:-}" ]]; then
  EXTRA_ARGS+=(--checkpoint-path "${CHECKPOINT_PATH}")
fi

"${PYTHON_BIN}" -m oops_temporal.extract_i3d_features \
  --annotations-dir "${ANNOTATIONS_DIR:-datasets/oops/annotations}" \
  --video-root "${VIDEO_ROOT:-datasets/oops/raw/oops_video}" \
  --features-dir "${FEATURES_DIR:-datasets/oops/features/i3d_r50_8fps16_stride4}" \
  --backbone "${BACKBONE:-pytorchvideo_i3d_r50}" \
  --sample-fps "${SAMPLE_FPS:-8}" \
  --clip-len "${CLIP_LEN:-16}" \
  --stride-frames "${STRIDE_FRAMES:-4}" \
  --batch-size "${BATCH_SIZE:-16}" \
  --num-shards "${NUM_SHARDS:-1}" \
  --shard-index "${SHARD_INDEX:-0}" \
  "${EXTRA_ARGS[@]}" \
  "$@"
