from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .data import build_samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate simple non-video Oops baselines.")
    parser.add_argument("--annotations-dir", default="datasets/oops/annotations")
    parser.add_argument("--output", default="runs/oops_temporal/baselines.json")
    return parser.parse_args()


def metrics(pred: np.ndarray, target: np.ndarray, duration: np.ndarray) -> dict[str, float]:
    err = np.abs(pred - target)
    return {
        "mae_rel": float(err.mean()),
        "median_ae_rel": float(np.median(err)),
        "mae_sec": float((err * duration).mean()),
        "acc_005": float((err <= 0.05).mean()),
        "acc_010": float((err <= 0.10).mean()),
        "acc_020": float((err <= 0.20).mean()),
    }


def main() -> None:
    args = parse_args()
    train = build_samples(args.annotations_dir, split="train", filtered=True)
    val = build_samples(args.annotations_dir, split="val", filtered=True)
    train_rel = np.array([s.target_rel for s in train], dtype=np.float64)
    val_rel = np.array([s.target_rel for s in val], dtype=np.float64)
    val_duration = np.array([s.duration for s in val], dtype=np.float64)

    mean_pred = np.full_like(val_rel, train_rel.mean())
    median_pred = np.full_like(val_rel, np.median(train_rel))
    center_pred = np.full_like(val_rel, 0.5)
    rows = {
        "center_0.5": metrics(center_pred, val_rel, val_duration),
        "train_mean": metrics(mean_pred, val_rel, val_duration),
        "train_median": metrics(median_pred, val_rel, val_duration),
        "train_count": len(train),
        "val_count": len(val),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, sort_keys=True)
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
