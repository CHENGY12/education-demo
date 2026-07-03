#!/usr/bin/env bash
set -euo pipefail

DATASET_DIR="${DATASET_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
JOBS="${JOBS:-4}"
TIMEOUT="${TIMEOUT:-30}"

usage() {
  cat <<'EOF'
Download the Oops! Unintentional Action dataset.

Usage:
  ./download_oops.sh [main|captions|models|flow|all]

Defaults:
  main      Videos and annotations, about 45GB

Optional environment variables:
  DATASET_DIR=/path/to/output
  JOBS=4
  TIMEOUT=30

Notes:
  flow is about 1019GB and is only downloaded when explicitly requested.
EOF
}

download_one() {
  local url="$1"
  local output="$2"

  mkdir -p "$DATASET_DIR"
  echo "Downloading $output"
  echo "  from: $url"
  echo "  to:   $DATASET_DIR/$output"

  if command -v aria2c >/dev/null 2>&1; then
    aria2c \
      --continue=true \
      --max-connection-per-server="$JOBS" \
      --split="$JOBS" \
      --connect-timeout="$TIMEOUT" \
      --timeout="$TIMEOUT" \
      --dir="$DATASET_DIR" \
      --out="$output" \
      "$url"
  else
    curl \
      --location \
      --continue-at - \
      --connect-timeout "$TIMEOUT" \
      --retry 10 \
      --retry-delay 10 \
      --output "$DATASET_DIR/$output" \
      "$url"
  fi
}

target="${1:-main}"

case "$target" in
  -h|--help|help)
    usage
    ;;
  main)
    download_one "https://oops.cs.columbia.edu/data/video_and_anns.tar.gz" "video_and_anns.tar.gz"
    ;;
  captions)
    download_one "https://oops.cs.columbia.edu/data/lang.tar.gz" "lang.tar.gz"
    ;;
  models)
    download_one "https://oops.cs.columbia.edu/data/models.tar.gz" "models.tar.gz"
    ;;
  flow)
    download_one "https://oops.cs.columbia.edu/data/flow.tar.gz" "flow.tar.gz"
    ;;
  all)
    download_one "https://oops.cs.columbia.edu/data/video_and_anns.tar.gz" "video_and_anns.tar.gz"
    download_one "https://oops.cs.columbia.edu/data/lang.tar.gz" "lang.tar.gz"
    download_one "https://oops.cs.columbia.edu/data/models.tar.gz" "models.tar.gz"
    download_one "https://oops.cs.columbia.edu/data/flow.tar.gz" "flow.tar.gz"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
