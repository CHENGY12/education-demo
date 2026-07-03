from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import numpy as np
import torch
import torch.nn.functional as F
from tqdm import tqdm

from .data import OopsSample, build_samples, sanitize_video_id


VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".avi", ".mov")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract frozen video features for Oops temporal localization.")
    parser.add_argument("--annotations-dir", default="datasets/oops/annotations")
    parser.add_argument("--video-root", default="datasets/oops/raw/oops_video")
    parser.add_argument("--features-dir", default="datasets/oops/features/i3d_r50")
    parser.add_argument("--splits", nargs="+", default=["train", "val"], choices=["train", "val"])
    parser.add_argument(
        "--backbone",
        choices=[
            "pytorchvideo_i3d_r50",
            "inception_i3d_imagenet",
            "inception_i3d_fvd400",
            "fvd_i3d_torchscript",
            "torchvision_r3d18",
        ],
        default="pytorchvideo_i3d_r50",
    )
    parser.add_argument("--checkpoint-path", default=None, help="Optional local checkpoint override for the selected backbone.")
    parser.add_argument("--i3d-py-path", default="models/i3d_carrotbu/i3d.py")
    parser.add_argument("--sample-fps", type=float, default=8.0)
    parser.add_argument("--clip-len", type=int, default=16)
    parser.add_argument("--stride-frames", type=int, default=4)
    parser.add_argument("--resize-shorter", type=int, default=256)
    parser.add_argument("--crop-size", type=int, default=224)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--filtered", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--failures-jsonl", default=None)
    return parser.parse_args()


class PytorchVideoI3DFeatureExtractor(torch.nn.Module):
    def __init__(self, checkpoint_path: str | None = None) -> None:
        super().__init__()
        try:
            model = torch.hub.load(
                "facebookresearch/pytorchvideo",
                "i3d_r50",
                pretrained=checkpoint_path is None,
                trust_repo=True,
            )
        except TypeError:
            model = torch.hub.load("facebookresearch/pytorchvideo", "i3d_r50", pretrained=checkpoint_path is None)
        except Exception as exc:
            raise RuntimeError(
                "Could not load PyTorchVideo i3d_r50. Install/download dependencies first, "
                "or run with --backbone torchvision_r3d18 as an explicit non-I3D fallback."
            ) from exc
        if not hasattr(model, "blocks"):
            raise RuntimeError("unexpected PyTorchVideo i3d_r50 structure: missing .blocks")
        if checkpoint_path is not None:
            checkpoint = torch.load(checkpoint_path, map_location="cpu")
            state_dict = checkpoint.get("model_state", checkpoint.get("state_dict", checkpoint))
            model.load_state_dict(state_dict)
        self.blocks = torch.nn.ModuleList(list(model.blocks)[:-1])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for block in self.blocks:
            x = block(x)
        x = F.adaptive_avg_pool3d(x, output_size=(1, 1, 1))
        return x.flatten(1)


def load_python_module(path: str | Path, module_name: str):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"missing Python module: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InceptionI3DFeatureExtractor(torch.nn.Module):
    def __init__(self, i3d_py_path: str | Path, checkpoint_path: str | Path) -> None:
        super().__init__()
        module = load_python_module(i3d_py_path, "oops_external_i3d")
        model = module.InceptionI3d(num_classes=400, in_channels=3)
        checkpoint = torch.load(checkpoint_path, map_location="cpu")
        state_dict = checkpoint.get("model_state", checkpoint.get("state_dict", checkpoint)) if isinstance(checkpoint, dict) else checkpoint
        model.load_state_dict(state_dict, strict=True)
        self.model = model

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.model.extract_features(x)
        x = F.adaptive_avg_pool3d(x, output_size=(1, 1, 1))
        return x.flatten(1)


class FVDI3DTorchscriptFeatureExtractor(torch.nn.Module):
    def __init__(self, checkpoint_path: str | Path) -> None:
        super().__init__()
        self.model = torch.jit.load(str(checkpoint_path), map_location="cpu")

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.model(x, True, False, True)
        if out.ndim > 2:
            out = F.adaptive_avg_pool3d(out, output_size=(1, 1, 1)).flatten(1)
        return out.float()


class TorchvisionR3D18FeatureExtractor(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        from torchvision.models.video import R3D_18_Weights, r3d_18

        weights = R3D_18_Weights.KINETICS400_V1
        model = r3d_18(weights=weights)
        self.body = torch.nn.Sequential(*list(model.children())[:-1])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.body(x).flatten(1)


def build_backbone(
    name: str,
    checkpoint_path: str | None = None,
    i3d_py_path: str | Path = "models/i3d_carrotbu/i3d.py",
) -> tuple[torch.nn.Module, tuple[float, float, float], tuple[float, float, float]]:
    if name == "pytorchvideo_i3d_r50":
        return PytorchVideoI3DFeatureExtractor(checkpoint_path), (0.45, 0.45, 0.45), (0.225, 0.225, 0.225)
    if name == "inception_i3d_imagenet":
        ckpt = checkpoint_path or "models/i3d_carrotbu/rgb_imagenet.pt"
        return InceptionI3DFeatureExtractor(i3d_py_path, ckpt), (0.5, 0.5, 0.5), (0.5, 0.5, 0.5)
    if name == "inception_i3d_fvd400":
        ckpt = checkpoint_path or "models/i3d_xiaodong_fvd/i3d_pretrained_400.pt"
        return InceptionI3DFeatureExtractor(i3d_py_path, ckpt), (0.5, 0.5, 0.5), (0.5, 0.5, 0.5)
    if name == "fvd_i3d_torchscript":
        ckpt = checkpoint_path or "models/i3d_fvd_torchscript/i3d_torchscript.pt"
        return FVDI3DTorchscriptFeatureExtractor(ckpt), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    if name == "torchvision_r3d18":
        return TorchvisionR3D18FeatureExtractor(), (0.43216, 0.394666, 0.37645), (0.22803, 0.22145, 0.216989)
    raise ValueError(f"unknown backbone: {name}")


def import_imageio():
    try:
        import imageio.v2 as imageio
    except ImportError as exc:
        raise RuntimeError(
            "Video decoding requires imageio and imageio-ffmpeg in this environment. "
            "Install them in the ROCm env before running feature extraction."
        ) from exc
    return imageio


def output_path(features_dir: Path, video_id: str) -> Path:
    return features_dir / f"{sanitize_video_id(video_id)}.pt"


def candidate_paths(video_root: Path, split: str, video_id: str) -> Iterable[Path]:
    for ext in VIDEO_EXTS:
        yield video_root / split / f"{video_id}{ext}"
        yield video_root / f"{video_id}{ext}"


def build_video_index(video_root: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    if not video_root.exists():
        return index
    for path in video_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTS:
            index.setdefault(path.name, path)
            index.setdefault(path.stem, path)
    return index


def resolve_video_path(video_root: Path, split: str, video_id: str, index: dict[str, Path]) -> Path | None:
    for path in candidate_paths(video_root, split, video_id):
        if path.exists():
            return path
    for ext in VIDEO_EXTS:
        path = index.get(f"{video_id}{ext}")
        if path is not None:
            return path
    return index.get(video_id)


def decode_video_at_fps(path: Path, sample_fps: float) -> tuple[torch.Tensor, float, dict]:
    imageio = import_imageio()
    reader = imageio.get_reader(str(path), "ffmpeg")
    frames: list[np.ndarray] = []
    meta: dict = {}
    try:
        meta = dict(reader.get_meta_data() or {})
        src_fps = float(meta.get("fps") or sample_fps)
        step = max(int(round(src_fps / sample_fps)), 1)
        for frame_idx, frame in enumerate(reader):
            if frame_idx % step != 0:
                continue
            if frame.ndim == 2:
                frame = np.repeat(frame[..., None], 3, axis=-1)
            frame = np.ascontiguousarray(frame[..., :3])
            frames.append(frame)
    finally:
        reader.close()

    if not frames:
        raise RuntimeError(f"decoded zero frames from {path}")

    array = np.stack(frames, axis=0)
    tensor = torch.from_numpy(array).permute(0, 3, 1, 2).float().div_(255.0)
    effective_fps = min(float(meta.get("fps") or sample_fps), sample_fps)
    return tensor, effective_fps, meta


def resize_center_crop(frames: torch.Tensor, resize_shorter: int, crop_size: int) -> torch.Tensor:
    _, _, height, width = frames.shape
    if height <= 0 or width <= 0:
        raise RuntimeError(f"invalid frame shape: {tuple(frames.shape)}")
    if height < width:
        new_height = resize_shorter
        new_width = int(round(width * resize_shorter / height))
    else:
        new_width = resize_shorter
        new_height = int(round(height * resize_shorter / width))
    frames = F.interpolate(frames, size=(new_height, new_width), mode="bilinear", align_corners=False)
    top = max((new_height - crop_size) // 2, 0)
    left = max((new_width - crop_size) // 2, 0)
    frames = frames[:, :, top : top + crop_size, left : left + crop_size]
    if frames.shape[-2:] != (crop_size, crop_size):
        frames = F.interpolate(frames, size=(crop_size, crop_size), mode="bilinear", align_corners=False)
    return frames.contiguous()


def normalize(frames: torch.Tensor, mean: tuple[float, float, float], std: tuple[float, float, float]) -> torch.Tensor:
    mean_t = torch.tensor(mean, dtype=frames.dtype).view(1, 3, 1, 1)
    std_t = torch.tensor(std, dtype=frames.dtype).view(1, 3, 1, 1)
    return (frames - mean_t) / std_t


def make_clips(frames: torch.Tensor, fps: float, clip_len: int, stride_frames: int) -> tuple[torch.Tensor, torch.Tensor]:
    total = int(frames.shape[0])
    stride_frames = max(int(stride_frames), 1)
    if total <= clip_len:
        starts = [0]
    else:
        starts = list(range(0, total - clip_len + 1, stride_frames))
        last = total - clip_len
        if starts[-1] != last:
            starts.append(last)

    clips = []
    times = []
    for start in starts:
        idx = torch.arange(start, start + clip_len).clamp_max(total - 1)
        clips.append(frames.index_select(0, idx))
        center_frame = float(idx.float().mean().item())
        times.append(center_frame / max(fps, 1e-6))
    clip_tensor = torch.stack(clips, dim=0).permute(0, 2, 1, 3, 4).contiguous()
    return clip_tensor, torch.tensor(times, dtype=torch.float32)


@torch.inference_mode()
def extract_one(
    model: torch.nn.Module,
    video_path: Path,
    args: argparse.Namespace,
    mean: tuple[float, float, float],
    std: tuple[float, float, float],
    device: torch.device,
) -> tuple[torch.Tensor, torch.Tensor, dict]:
    frames, fps, decode_meta = decode_video_at_fps(video_path, args.sample_fps)
    frames = resize_center_crop(frames, args.resize_shorter, args.crop_size)
    frames = normalize(frames, mean, std)
    clips, times_sec = make_clips(frames, fps, args.clip_len, args.stride_frames)

    feats = []
    for start in range(0, clips.shape[0], args.batch_size):
        batch = clips[start : start + args.batch_size].to(device, non_blocking=True)
        out = model(batch).detach().float().cpu()
        feats.append(out)
    return torch.cat(feats, dim=0), times_sec, decode_meta


def iter_samples(args: argparse.Namespace) -> list[tuple[str, OopsSample]]:
    if args.num_shards < 1:
        raise ValueError("--num-shards must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.num_shards:
        raise ValueError("--shard-index must be in [0, num_shards)")

    rows: list[tuple[str, OopsSample]] = []
    for split in args.splits:
        samples = build_samples(args.annotations_dir, split=split, filtered=args.filtered)
        rows.extend((split, sample) for sample in samples)
    if args.num_shards > 1:
        rows = [row for idx, row in enumerate(rows) if idx % args.num_shards == args.shard_index]
    if args.limit is not None:
        rows = rows[: args.limit]
    return rows


def log_failure(path: Path | None, row: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def main() -> None:
    args = parse_args()
    torch.set_float32_matmul_precision("high")
    device = torch.device(args.device)
    video_root = Path(args.video_root)
    features_dir = Path(args.features_dir)
    features_dir.mkdir(parents=True, exist_ok=True)
    failures_path = Path(args.failures_jsonl) if args.failures_jsonl else features_dir / "failures.jsonl"

    print(
        json.dumps(
            {
                "backbone": args.backbone,
                "video_root": str(video_root),
                "features_dir": str(features_dir),
                "device": str(device),
                "sample_fps": args.sample_fps,
                "clip_len": args.clip_len,
                "stride_frames": args.stride_frames,
                "num_shards": args.num_shards,
                "shard_index": args.shard_index,
            },
            indent=2,
        )
    )

    model, mean, std = build_backbone(args.backbone, args.checkpoint_path, args.i3d_py_path)
    model = model.eval().to(device)
    samples = iter_samples(args)
    index = build_video_index(video_root)

    stats = {"saved": 0, "skipped": 0, "missing": 0, "failed": 0}
    t0 = time.time()
    for split, sample in tqdm(samples, desc="extract"):
        out_path = output_path(features_dir, sample.video_id)
        if out_path.exists() and not args.force:
            stats["skipped"] += 1
            continue

        video_path = resolve_video_path(video_root, split, sample.video_id, index)
        if video_path is None:
            stats["missing"] += 1
            log_failure(failures_path, {"video_id": sample.video_id, "split": split, "error": "missing_video"})
            continue

        try:
            features, times_sec, decode_meta = extract_one(model, video_path, args, mean, std, device)
            payload = {
                "features": features,
                "times_sec": times_sec,
                "video_id": sample.video_id,
                "split": split,
                "source_path": str(video_path),
                "sample": asdict(sample),
                "backbone": args.backbone,
                "feature_dim": int(features.shape[-1]),
                "decode_meta": {k: v for k, v in decode_meta.items() if isinstance(v, (str, int, float, bool))},
                "extract_args": vars(args),
            }
            tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
            torch.save(payload, tmp_path)
            tmp_path.replace(out_path)
            stats["saved"] += 1
        except Exception as exc:
            stats["failed"] += 1
            log_failure(
                failures_path,
                {
                    "video_id": sample.video_id,
                    "split": split,
                    "path": str(video_path),
                    "error": repr(exc),
                },
            )
            print(f"failed {sample.video_id}: {exc}", file=sys.stderr)

    stats["seconds"] = round(time.time() - t0, 3)
    print(json.dumps(stats, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
