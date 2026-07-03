#!/usr/bin/env bash
set -euo pipefail

OUTER_ARCHIVE="${1:-datasets/oops/hf_pengxiang/video_and_anns.tar.gz}"
RAW_DIR="${2:-datasets/oops/raw}"
INNER_ARCHIVE="${RAW_DIR}/oops_dataset/video.tar.gz"
VIDEO_ROOT="${RAW_DIR}/oops_video"
MARKER="${RAW_DIR}/.oops_video_extract_complete"

mkdir -p "${RAW_DIR}"

if [[ ! -s "${INNER_ARCHIVE}" ]]; then
  echo "extracting ${INNER_ARCHIVE} from ${OUTER_ARCHIVE}"
  tar -xzf "${OUTER_ARCHIVE}" -C "${RAW_DIR}" oops_dataset/video.tar.gz
else
  echo "found existing ${INNER_ARCHIVE}"
fi

if [[ -f "${MARKER}" && -d "${VIDEO_ROOT}" ]]; then
  echo "video extraction already marked complete: ${VIDEO_ROOT}"
else
  echo "extracting videos into ${RAW_DIR}"
  tar -xzf "${INNER_ARCHIVE}" -C "${RAW_DIR}"
  touch "${MARKER}"
fi

if [[ -d "${VIDEO_ROOT}" ]]; then
  echo "mp4_count: $(find "${VIDEO_ROOT}" -type f -name '*.mp4' | wc -l)"
else
  echo "missing expected video root: ${VIDEO_ROOT}" >&2
  exit 1
fi
