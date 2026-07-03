from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import torch
from torch.utils.data import Dataset


@dataclass(frozen=True)
class OopsSample:
    video_id: str
    duration: float
    target_sec: float
    target_rel: float
    rel_stdev: float
    n_valid: int
    n_notfound: int


def read_split(path: str | Path) -> list[str]:
    with Path(path).open("r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def load_transition_times(path: str | Path) -> dict[str, dict]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def sanitize_video_id(video_id: str) -> str:
    """Filesystem-safe deterministic name for feature files."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", video_id)
    return name.strip("._") or "video"


def valid_target(entry: dict) -> tuple[float, float, int]:
    rel_times = [float(t) for t in entry["rel_t"] if float(t) >= 0.0]
    abs_times = [float(t) for t in entry["t"] if float(t) >= 0.0]
    if not rel_times or not abs_times:
        return math.nan, math.nan, 0
    target_rel = float(np.mean(rel_times))
    target_sec = float(np.mean(abs_times))
    return target_rel, target_sec, len(rel_times)


def build_samples(
    annotations_dir: str | Path,
    split: str = "train",
    filtered: bool = True,
    drop_invalid: bool = True,
) -> list[OopsSample]:
    annotations_dir = Path(annotations_dir)
    split_name = f"{split}_filtered.txt" if filtered else f"{split}.txt"
    ids = read_split(annotations_dir / split_name)
    transitions = load_transition_times(annotations_dir / "transition_times.json")

    samples: list[OopsSample] = []
    missing = 0
    invalid = 0
    for video_id in ids:
        entry = transitions.get(video_id)
        if entry is None:
            missing += 1
            continue
        target_rel, target_sec, n_valid = valid_target(entry)
        if n_valid == 0:
            invalid += 1
            if drop_invalid:
                continue
        duration = float(entry["len"])
        rel_stdev = float(entry.get("rel_stdev", 0.0))
        n_notfound = int(entry.get("n_notfound", 0))
        samples.append(
            OopsSample(
                video_id=video_id,
                duration=duration,
                target_sec=target_sec,
                target_rel=float(np.clip(target_rel, 0.0, 1.0)),
                rel_stdev=max(rel_stdev, 0.0),
                n_valid=n_valid,
                n_notfound=n_notfound,
            )
        )
    if missing:
        print(f"warning: skipped {missing} split ids missing from transition_times.json")
    if invalid and drop_invalid:
        print(f"warning: skipped {invalid} split ids with no valid transition annotation")
    return samples


def gaussian_label(
    center_rel: float,
    num_bins: int,
    sigma_bins: float = 2.0,
    min_sigma_bins: float = 1.0,
) -> torch.Tensor:
    idx = torch.arange(num_bins, dtype=torch.float32)
    center = float(np.clip(center_rel, 0.0, 1.0)) * (num_bins - 1)
    sigma = max(float(sigma_bins), float(min_sigma_bins))
    label = torch.exp(-0.5 * ((idx - center) / sigma) ** 2)
    return label / label.sum().clamp_min(1e-12)


def time_basis(num_bins: int) -> torch.Tensor:
    t = torch.linspace(0.0, 1.0, num_bins)
    return torch.stack(
        [
            t,
            torch.sin(2.0 * math.pi * t),
            torch.cos(2.0 * math.pi * t),
            torch.sin(4.0 * math.pi * t),
            torch.cos(4.0 * math.pi * t),
        ],
        dim=-1,
    )


def metadata_basis(sample: OopsSample, num_bins: int) -> torch.Tensor:
    duration_norm = min(sample.duration / 30.0, 1.0)
    log_duration_norm = math.log1p(max(sample.duration, 0.0)) / math.log1p(30.0)
    meta = torch.tensor([duration_norm, log_duration_norm], dtype=torch.float32)
    return meta.expand(num_bins, -1)


def _feature_candidates(features_dir: Path, video_id: str) -> Iterable[Path]:
    safe = sanitize_video_id(video_id)
    for stem in (video_id, safe):
        yield features_dir / f"{stem}.pt"
        yield features_dir / f"{stem}.npy"


def feature_file_exists(features_dir: str | Path, video_id: str) -> bool:
    features_path = Path(features_dir)
    return any(path.exists() for path in _feature_candidates(features_path, video_id))


def load_feature_file(features_dir: Path, video_id: str) -> torch.Tensor:
    for path in _feature_candidates(features_dir, video_id):
        if not path.exists():
            continue
        if path.suffix == ".pt":
            feat = torch.load(path, map_location="cpu")
            if isinstance(feat, dict):
                for key in ("features", "feat", "x"):
                    if key in feat:
                        feat = feat[key]
                        break
            feat = torch.as_tensor(feat, dtype=torch.float32)
        elif path.suffix == ".npy":
            feat = torch.from_numpy(np.load(path)).float()
        else:
            continue
        if feat.ndim != 2:
            raise ValueError(f"expected [time, dim] features in {path}, got shape {tuple(feat.shape)}")
        return feat
    raise FileNotFoundError(f"no .pt/.npy feature file found for {video_id!r} in {features_dir}")


def resample_sequence(x: torch.Tensor, num_bins: int) -> torch.Tensor:
    if x.shape[0] == num_bins:
        return x
    x_t = x.T.unsqueeze(0)
    y = torch.nn.functional.interpolate(x_t, size=num_bins, mode="linear", align_corners=True)
    return y.squeeze(0).T.contiguous()


class OopsTemporalDataset(Dataset):
    def __init__(
        self,
        samples: list[OopsSample],
        num_bins: int = 64,
        label_sigma_bins: float = 2.0,
        features_dir: str | Path | None = None,
        feature_mode: str = "time",
        append_time_features: bool = False,
    ) -> None:
        if feature_mode not in {"time", "metadata", "features"}:
            raise ValueError(f"unknown feature_mode: {feature_mode}")
        if feature_mode == "features" and features_dir is None:
            raise ValueError("features_dir is required when feature_mode='features'")

        self.samples = samples
        self.num_bins = int(num_bins)
        self.label_sigma_bins = float(label_sigma_bins)
        self.features_dir = Path(features_dir) if features_dir else None
        self.feature_mode = feature_mode
        self.append_time_features = bool(append_time_features)
        self._time_basis = time_basis(self.num_bins)

    @property
    def feature_dim(self) -> int:
        if self.feature_mode == "time":
            return int(self._time_basis.shape[-1])
        if self.feature_mode == "metadata":
            return int(self._time_basis.shape[-1] + 2)
        assert self.features_dir is not None
        first = load_feature_file(self.features_dir, self.samples[0].video_id)
        dim = int(first.shape[-1])
        if self.append_time_features:
            dim += int(self._time_basis.shape[-1] + 2)
        return dim

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor | str | float]:
        sample = self.samples[idx]
        if self.feature_mode == "time":
            features = self._time_basis.clone()
        elif self.feature_mode == "metadata":
            features = torch.cat([self._time_basis, metadata_basis(sample, self.num_bins)], dim=-1)
        else:
            assert self.features_dir is not None
            features = resample_sequence(load_feature_file(self.features_dir, sample.video_id), self.num_bins)
            if self.append_time_features:
                features = torch.cat([features, self._time_basis, metadata_basis(sample, self.num_bins)], dim=-1)

        sigma = max(self.label_sigma_bins, sample.rel_stdev * self.num_bins)
        label = gaussian_label(sample.target_rel, self.num_bins, sigma_bins=sigma)
        target_bin = int(round(sample.target_rel * (self.num_bins - 1)))
        return {
            "video_id": sample.video_id,
            "features": features,
            "label": label,
            "target_rel": torch.tensor(sample.target_rel, dtype=torch.float32),
            "target_sec": torch.tensor(sample.target_sec, dtype=torch.float32),
            "duration": torch.tensor(sample.duration, dtype=torch.float32),
            "target_bin": torch.tensor(target_bin, dtype=torch.long),
        }


def collate_batch(batch: list[dict]) -> dict:
    return {
        "video_id": [item["video_id"] for item in batch],
        "features": torch.stack([item["features"] for item in batch]),
        "label": torch.stack([item["label"] for item in batch]),
        "target_rel": torch.stack([item["target_rel"] for item in batch]),
        "target_sec": torch.stack([item["target_sec"] for item in batch]),
        "duration": torch.stack([item["duration"] for item in batch]),
        "target_bin": torch.stack([item["target_bin"] for item in batch]),
    }
