from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .data import build_samples, sanitize_video_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a CSV index for Oops temporal localization.")
    parser.add_argument("--annotations-dir", default="datasets/oops/annotations")
    parser.add_argument("--output", default="datasets/oops/oops_index.csv")
    parser.add_argument("--unfiltered", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = []
    for split in ("train", "val"):
        samples = build_samples(args.annotations_dir, split=split, filtered=not args.unfiltered)
        for sample in samples:
            rows.append(
                {
                    "split": split,
                    "video_id": sample.video_id,
                    "safe_id": sanitize_video_id(sample.video_id),
                    "duration": sample.duration,
                    "target_sec": sample.target_sec,
                    "target_rel": sample.target_rel,
                    "rel_stdev": sample.rel_stdev,
                    "n_valid": sample.n_valid,
                    "n_notfound": sample.n_notfound,
                }
            )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {output}")


if __name__ == "__main__":
    main()

