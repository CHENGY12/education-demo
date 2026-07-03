#!/usr/bin/env bash
set -euo pipefail

cd /vast/users/guangyi.chen/causal_group/guangyi.chen

PYTHON_BIN="${PYTHON_BIN:-/vast/users/guangyi.chen/anaconda3/envs/simulation-hessian2-rocm/bin/python}"
FEATURES_DIR="${FEATURES_DIR:-datasets/oops/features/inception_i3d_fvd400_8fps16_stride4}"
RUN_DIR="${RUN_DIR:-runs/oops_temporal/inception_i3d_fvd400_8fps16_stride4_transformer}"
REPORT_JSON="${REPORT_JSON:-runs/oops_temporal/inception_i3d_fvd400_8fps16_stride4_feature_report.json}"
EXTRACT_JOB_ID="${EXTRACT_JOB_ID:-75708}"
EXPECTED_FEATURES="${EXPECTED_FEATURES:-9183}"
MIN_FEATURES="${MIN_FEATURES:-8500}"

export PYTHONNOUSERSITE=1
export PYTHONPATH="${PWD}:${PYTHONPATH:-}"
export TORCH_HOME="${TORCH_HOME:-${PWD}/.cache/torch}"
export MIOPEN_USER_DB_PATH="${MIOPEN_USER_DB_PATH:-${PWD}/.cache/miopen_user_db_train}"
export MIOPEN_CUSTOM_CACHE_DIR="${MIOPEN_CUSTOM_CACHE_DIR:-${PWD}/.cache/miopen_custom_cache_train}"
mkdir -p "${TORCH_HOME}" "${MIOPEN_USER_DB_PATH}" "${MIOPEN_CUSTOM_CACHE_DIR}" logs runs/oops_temporal

echo "[wait] waiting for extraction job ${EXTRACT_JOB_ID}"
while squeue -j "${EXTRACT_JOB_ID}" -h | grep -q .; do
  count="$(find "${FEATURES_DIR}" -maxdepth 1 -type f -name '*.pt' | wc -l)"
  echo "[wait] $(date -Is) features=${count}/${EXPECTED_FEATURES}"
  sleep 120
done

count="$(find "${FEATURES_DIR}" -maxdepth 1 -type f -name '*.pt' | wc -l)"
echo "[check] extraction job finished, features=${count}/${EXPECTED_FEATURES}, minimum=${MIN_FEATURES}"
if [[ "${count}" -lt "${MIN_FEATURES}" ]]; then
  echo "[error] too many missing features; aborting training"
  exit 2
fi
if [[ "${count}" -lt "${EXPECTED_FEATURES}" ]]; then
  echo "[warn] some videos failed feature extraction; feature_report and training will skip missing samples"
fi

echo "[report] writing ${REPORT_JSON}"
"${PYTHON_BIN}" -m oops_temporal.feature_report \
  --features-dir "${FEATURES_DIR}" \
  --output-json "${REPORT_JSON}"

echo "[train] writing ${RUN_DIR}"
"${PYTHON_BIN}" -m oops_temporal.train \
  --annotations-dir datasets/oops/annotations \
  --feature-mode features \
  --features-dir "${FEATURES_DIR}" \
  --model transformer \
  --num-bins 128 \
  --hidden-dim 256 \
  --num-layers 4 \
  --num-heads 4 \
  --batch-size 128 \
  --epochs 30 \
  --lr 3e-4 \
  --weight-decay 0.05 \
  --num-workers 0 \
  --amp \
  --skip-missing-features \
  --output-dir "${RUN_DIR}"

echo "[done] training complete"
