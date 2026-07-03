#!/usr/bin/env bash
set -euo pipefail

JOB_ID="${JOB_ID:-75639}"
NUM_SHARDS="${NUM_SHARDS:-8}"
PYTHON_BIN="${PYTHON_BIN:-/vast/users/guangyi.chen/anaconda3/envs/simulation-hessian2-rocm/bin/python}"
PROJECT_DIR="${PROJECT_DIR:-/vast/users/guangyi.chen/causal_group/guangyi.chen}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-datasets/oops/annotations}"
VIDEO_ROOT="${VIDEO_ROOT:-datasets/oops/raw/oops_video}"
FEATURES_DIR="${FEATURES_DIR:-datasets/oops/features/inception_i3d_fvd400_8fps16_stride4}"
BACKBONE="${BACKBONE:-inception_i3d_fvd400}"
SAMPLE_FPS="${SAMPLE_FPS:-8}"
CLIP_LEN="${CLIP_LEN:-16}"
STRIDE_FRAMES="${STRIDE_FRAMES:-4}"
BATCH_SIZE="${BATCH_SIZE:-16}"
TORCH_HOME_DIR="${TORCH_HOME:-${PROJECT_DIR}/.cache/torch}"

mkdir -p "${PROJECT_DIR}/logs" "${TORCH_HOME_DIR}"

export PROJECT_DIR PYTHON_BIN ANNOTATIONS_DIR VIDEO_ROOT FEATURES_DIR BACKBONE
export SAMPLE_FPS CLIP_LEN STRIDE_FRAMES BATCH_SIZE TORCH_HOME_DIR NUM_SHARDS
export CHECKPOINT_PATH="${CHECKPOINT_PATH:-}"

srun --jobid="${JOB_ID}" -n "${NUM_SHARDS}" bash -lc '
  set -euo pipefail
  rank="${SLURM_PROCID:-0}"
  cd "${PROJECT_DIR}"
  log_path="${PROJECT_DIR}/logs/oops_i3d_srun_shard_${rank}_of_${NUM_SHARDS}.log"
  exec >"${log_path}" 2>&1

  miopen_dir="${PROJECT_DIR}/.cache/miopen_srun_shard_${rank}"
  mkdir -p "${miopen_dir}"
  export ROCR_VISIBLE_DEVICES="${rank}"
  export HIP_VISIBLE_DEVICES="${rank}"
  export TORCH_HOME="${TORCH_HOME_DIR}"
  export MIOPEN_USER_DB_PATH="${miopen_dir}"
  export MIOPEN_CUSTOM_CACHE_DIR="${miopen_dir}"

  extra_args=()
  if [[ -n "${CHECKPOINT_PATH}" ]]; then
    extra_args+=(--checkpoint-path "${CHECKPOINT_PATH}")
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
    --shard-index "${rank}" \
    "${extra_args[@]}" \
    "$@"
' bash "$@"
