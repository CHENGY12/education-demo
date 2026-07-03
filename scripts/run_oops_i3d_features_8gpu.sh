#!/usr/bin/env bash
set -euo pipefail

JOB_ID="${JOB_ID:-75639}"
NUM_SHARDS="${NUM_SHARDS:-8}"
PYTHON_BIN="${PYTHON_BIN:-/vast/users/guangyi.chen/anaconda3/envs/simulation-hessian2-rocm/bin/python}"
PROJECT_DIR="${PROJECT_DIR:-/vast/users/guangyi.chen/causal_group/guangyi.chen}"
ANNOTATIONS_DIR="${ANNOTATIONS_DIR:-datasets/oops/annotations}"
VIDEO_ROOT="${VIDEO_ROOT:-datasets/oops/raw/oops_video}"
FEATURES_DIR="${FEATURES_DIR:-datasets/oops/features/i3d_r50_8fps16_stride4}"
BACKBONE="${BACKBONE:-pytorchvideo_i3d_r50}"
SAMPLE_FPS="${SAMPLE_FPS:-8}"
CLIP_LEN="${CLIP_LEN:-16}"
STRIDE_FRAMES="${STRIDE_FRAMES:-4}"
BATCH_SIZE="${BATCH_SIZE:-16}"
TORCH_HOME_DIR="${TORCH_HOME:-${PROJECT_DIR}/.cache/torch}"

mkdir -p "${PROJECT_DIR}/logs" "${TORCH_HOME_DIR}"

for shard in $(seq 0 "$((NUM_SHARDS - 1))"); do
  log_path="${PROJECT_DIR}/logs/oops_i3d_shard_${shard}_of_${NUM_SHARDS}.log"
  miopen_dir="${PROJECT_DIR}/.cache/miopen_shard_${shard}"
  mkdir -p "${miopen_dir}"
  (
    cd "${PROJECT_DIR}"
    srun --jobid="${JOB_ID}" --exclusive -N1 -n1 bash -lc "
      set -euo pipefail
      cd '${PROJECT_DIR}'
      export ROCR_VISIBLE_DEVICES='${shard}'
      export HIP_VISIBLE_DEVICES='${shard}'
      export TORCH_HOME='${TORCH_HOME_DIR}'
      export MIOPEN_USER_DB_PATH='${miopen_dir}'
      export MIOPEN_CUSTOM_CACHE_DIR='${miopen_dir}'
      export CHECKPOINT_PATH='${CHECKPOINT_PATH:-}'
      extra_args=()
      if [[ -n \"\${CHECKPOINT_PATH}\" ]]; then
        extra_args+=(--checkpoint-path \"\${CHECKPOINT_PATH}\")
      fi
      '${PYTHON_BIN}' -m oops_temporal.extract_i3d_features \
        --annotations-dir '${ANNOTATIONS_DIR}' \
        --video-root '${VIDEO_ROOT}' \
        --features-dir '${FEATURES_DIR}' \
        --backbone '${BACKBONE}' \
        --sample-fps '${SAMPLE_FPS}' \
        --clip-len '${CLIP_LEN}' \
        --stride-frames '${STRIDE_FRAMES}' \
        --batch-size '${BATCH_SIZE}' \
        --num-shards '${NUM_SHARDS}' \
        --shard-index '${shard}' \
        \"\${extra_args[@]}\" \
        \"\$@\"
    " bash "$@"
  ) >"${log_path}" 2>&1 &
  echo "started shard ${shard}/${NUM_SHARDS} -> ${log_path}"
done

wait
