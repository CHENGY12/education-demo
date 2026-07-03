from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import torch

from .data import build_samples, load_feature_file, sanitize_video_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Oops per-video feature files.")
    parser.add_argument("--annotations-dir", default="datasets/oops/annotations")
    parser.add_argument("--features-dir", required=True)
    parser.add_argument("--splits", nargs="+", default=["train", "val"], choices=["train", "val"])
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--output-json", default=None)
    return parser.parse_args()


def feature_path(features_dir: Path, video_id: str) -> Path | None:
    for stem in (video_id, sanitize_video_id(video_id)):
        for suffix in (".pt", ".npy"):
            path = features_dir / f"{stem}{suffix}"
            if path.exists():
                return path
    return None


def load_payload(path: Path) -> dict[str, Any]:
    if path.suffix == ".pt":
        payload = torch.load(path, map_location="cpu")
        if isinstance(payload, dict):
            return payload
        return {"features": payload}
    if path.suffix == ".npy":
        return {"features": np.load(path)}
    raise ValueError(f"unsupported feature file: {path}")


def summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "median": None, "max": None, "mean": None}
    arr = np.asarray(values, dtype=np.float64)
    return {
        "min": float(np.min(arr)),
        "median": float(np.median(arr)),
        "max": float(np.max(arr)),
        "mean": float(np.mean(arr)),
    }


def peak_metrics(features: torch.Tensor, times_sec: torch.Tensor | None, duration: float, target_rel: float) -> dict[str, float]:
    metrics: dict[str, float] = {}
    if features.shape[0] == 0:
        return metrics
    if times_sec is None or times_sec.numel() != features.shape[0]:
        times_rel = torch.linspace(0.0, 1.0, features.shape[0])
    else:
        times_rel = (times_sec.float() / max(float(duration), 1e-6)).clamp(0.0, 1.0)

    norm_peak = int(torch.linalg.vector_norm(features, dim=1).argmax().item())
    metrics["norm_peak_abs_err_rel"] = float(abs(float(times_rel[norm_peak]) - target_rel))
    if features.shape[0] > 1:
        delta = torch.linalg.vector_norm(features[1:] - features[:-1], dim=1)
        delta_peak = int(delta.argmax().item()) + 1
        metrics["delta_peak_abs_err_rel"] = float(abs(float(times_rel[delta_peak]) - target_rel))
    return metrics


def main() -> None:
    args = parse_args()
    features_dir = Path(args.features_dir)
    rows: list[dict[str, Any]] = []
    coverage: dict[str, dict[str, int]] = {}

    for split in args.splits:
        samples = build_samples(args.annotations_dir, split=split, filtered=True)
        present = []
        missing = 0
        for sample in samples:
            path = feature_path(features_dir, sample.video_id)
            if path is None:
                missing += 1
                continue
            present.append((split, sample, path))
        coverage[split] = {"total": len(samples), "present": len(present), "missing": missing}
        rows.extend(present)

    if args.max_files is not None:
        rows = rows[: args.max_files]

    dims: list[int] = []
    clips: list[int] = []
    finite_ratios: list[float] = []
    feature_stds: list[float] = []
    feature_norms: list[float] = []
    norm_peak_errors: list[float] = []
    delta_peak_errors: list[float] = []
    backbones: dict[str, int] = {}
    bad_files: list[dict[str, str]] = []

    for split, sample, path in rows:
        try:
            payload = load_payload(path)
            features = torch.as_tensor(payload["features"], dtype=torch.float32)
            if features.ndim != 2:
                raise ValueError(f"expected [time, dim], got {tuple(features.shape)}")
            finite = torch.isfinite(features)
            finite_ratio = float(finite.float().mean().item()) if features.numel() else 0.0
            dims.append(int(features.shape[1]))
            clips.append(int(features.shape[0]))
            finite_ratios.append(finite_ratio)
            feature_stds.append(float(features.std().item()) if features.numel() else math.nan)
            feature_norms.append(float(torch.linalg.vector_norm(features, dim=1).mean().item()) if features.numel() else math.nan)
            backbone = str(payload.get("backbone", "unknown"))
            backbones[backbone] = backbones.get(backbone, 0) + 1

            times = payload.get("times_sec")
            times_tensor = torch.as_tensor(times, dtype=torch.float32) if times is not None else None
            peaks = peak_metrics(features, times_tensor, sample.duration, sample.target_rel)
            if "norm_peak_abs_err_rel" in peaks:
                norm_peak_errors.append(peaks["norm_peak_abs_err_rel"])
            if "delta_peak_abs_err_rel" in peaks:
                delta_peak_errors.append(peaks["delta_peak_abs_err_rel"])
        except Exception as exc:
            bad_files.append({"split": split, "video_id": sample.video_id, "path": str(path), "error": repr(exc)})

    report = {
        "features_dir": str(features_dir),
        "coverage": coverage,
        "evaluated_files": len(rows),
        "bad_files": bad_files[:20],
        "bad_file_count": len(bad_files),
        "backbones": backbones,
        "dim_values": sorted(set(dims)),
        "clip_count": summarize([float(x) for x in clips]),
        "finite_ratio": summarize(finite_ratios),
        "feature_std": summarize(feature_stds),
        "feature_norm": summarize(feature_norms),
        "norm_peak_abs_err_rel": summarize(norm_peak_errors),
        "delta_peak_abs_err_rel": summarize(delta_peak_errors),
        "note": "Peak-error probes are weak sanity checks, not final localization metrics. Use training metrics for model quality.",
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.output_json:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
