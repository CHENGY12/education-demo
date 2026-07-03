from __future__ import annotations

import argparse
import gc
import json
import math
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from tqdm import tqdm

from .data import OopsTemporalDataset, build_samples, collate_batch, feature_file_exists
from .metrics import localization_metrics, soft_prediction_from_logits
from .model import build_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train an Oops temporal localization model.")
    parser.add_argument("--annotations-dir", default="datasets/oops/annotations")
    parser.add_argument("--features-dir", default=None, help="Directory with per-video .pt/.npy [T,D] features.")
    parser.add_argument("--feature-mode", choices=["time", "metadata", "features"], default="time")
    parser.add_argument("--output-dir", default="runs/oops_temporal/baseline_time")
    parser.add_argument("--model", choices=["mlp", "transformer"], default="mlp")
    parser.add_argument("--num-bins", type=int, default=64)
    parser.add_argument("--label-sigma-bins", type=float, default=2.0)
    parser.add_argument("--hidden-dim", type=int, default=128)
    parser.add_argument("--num-layers", type=int, default=3)
    parser.add_argument("--num-heads", type=int, default=4)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--append-time-features", action="store_true")
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=0.05)
    parser.add_argument("--warmup-ratio", type=float, default=0.05)
    parser.add_argument("--hard-ce-weight", type=float, default=0.0)
    parser.add_argument("--regression-loss-weight", type=float, default=0.0)
    parser.add_argument("--regression-temperature", type=float, default=1.0)
    parser.add_argument("--metric-prediction", choices=["argmax", "soft", "blend", "local"], default="argmax")
    parser.add_argument("--metric-temperature", type=float, default=1.0)
    parser.add_argument("--metric-argmax-weight", type=float, default=0.5)
    parser.add_argument("--metric-local-window", type=int, default=8)
    parser.add_argument("--save-best-metric", default="mae_rel")
    parser.add_argument("--save-best-mode", choices=["min", "max"], default="min")
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--amp", action="store_true")
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--limit-train", type=int, default=None)
    parser.add_argument("--limit-val", type=int, default=None)
    parser.add_argument("--overfit-batches", type=int, default=0)
    parser.add_argument("--skip-missing-features", action="store_true")
    return parser.parse_args()


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def make_loaders(args: argparse.Namespace) -> tuple[DataLoader, DataLoader, int]:
    train_samples = build_samples(args.annotations_dir, split="train", filtered=True)
    val_samples = build_samples(args.annotations_dir, split="val", filtered=True)
    if args.feature_mode == "features" and args.skip_missing_features:
        if not args.features_dir:
            raise ValueError("--features-dir is required with --skip-missing-features")
        train_samples = filter_samples_with_features(train_samples, args.features_dir, "train")
        val_samples = filter_samples_with_features(val_samples, args.features_dir, "val")
    if args.limit_train:
        train_samples = train_samples[: args.limit_train]
    if args.limit_val:
        val_samples = val_samples[: args.limit_val]
    if args.overfit_batches:
        n = args.overfit_batches * args.batch_size
        train_samples = train_samples[:n]
        val_samples = train_samples

    train_ds = OopsTemporalDataset(
        train_samples,
        num_bins=args.num_bins,
        label_sigma_bins=args.label_sigma_bins,
        features_dir=args.features_dir,
        feature_mode=args.feature_mode,
        append_time_features=args.append_time_features,
    )
    val_ds = OopsTemporalDataset(
        val_samples,
        num_bins=args.num_bins,
        label_sigma_bins=args.label_sigma_bins,
        features_dir=args.features_dir,
        feature_mode=args.feature_mode,
        append_time_features=args.append_time_features,
    )
    loader_kwargs = {
        "batch_size": args.batch_size,
        "num_workers": args.num_workers,
        "pin_memory": torch.cuda.is_available(),
        "collate_fn": collate_batch,
    }
    if args.num_workers > 0:
        loader_kwargs["persistent_workers"] = True
    train_loader = DataLoader(train_ds, shuffle=True, drop_last=False, **loader_kwargs)
    val_loader = DataLoader(val_ds, shuffle=False, drop_last=False, **loader_kwargs)
    return train_loader, val_loader, train_ds.feature_dim


def filter_samples_with_features(samples: list, features_dir: str | Path, split: str) -> list:
    kept = []
    missing = 0
    features_path = Path(features_dir)
    for sample in samples:
        if not feature_file_exists(features_path, sample.video_id):
            missing += 1
            continue
        kept.append(sample)
    print(f"feature coverage {split}: kept={len(kept)} missing={missing} dir={features_path}")
    if not kept:
        raise RuntimeError(f"no feature files found for split={split} in {features_path}")
    return kept


def cosine_lr(step: int, total_steps: int, base_lr: float, warmup_ratio: float) -> float:
    warmup = int(total_steps * warmup_ratio)
    if warmup > 0 and step < warmup:
        return base_lr * (step + 1) / warmup
    if total_steps <= warmup:
        return base_lr
    progress = (step - warmup) / max(total_steps - warmup, 1)
    return base_lr * 0.5 * (1.0 + math.cos(math.pi * progress))


def soft_ce_loss(logits: torch.Tensor, target_dist: torch.Tensor) -> torch.Tensor:
    log_prob = F.log_softmax(logits, dim=-1)
    return -(target_dist * log_prob).sum(dim=-1).mean()


def run_epoch(
    model: torch.nn.Module,
    loader: DataLoader,
    device: torch.device,
    optimizer: torch.optim.Optimizer | None = None,
    total_steps: int = 1,
    step: int = 0,
    args: argparse.Namespace | None = None,
) -> tuple[dict[str, float], int]:
    train = optimizer is not None
    model.train(train)
    totals: dict[str, float] = {}
    n_examples = 0

    autocast_enabled = bool(args and args.amp and device.type == "cuda")
    autocast_dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    iterator = tqdm(loader, leave=False, desc="train" if train else "val")
    for batch in iterator:
        features = batch["features"].to(device, non_blocking=True)
        labels = batch["label"].to(device, non_blocking=True)
        target_bin = batch["target_bin"].to(device, non_blocking=True)
        target_rel = batch["target_rel"].to(device, non_blocking=True)
        duration = batch["duration"].to(device, non_blocking=True)

        with torch.amp.autocast("cuda", dtype=autocast_dtype, enabled=autocast_enabled):
            logits = model(features)
            loss = soft_ce_loss(logits, labels)
            if args and args.hard_ce_weight > 0.0:
                loss = loss + args.hard_ce_weight * F.cross_entropy(logits, target_bin)
            if args and args.regression_loss_weight > 0.0:
                pred_rel = soft_prediction_from_logits(logits, temperature=args.regression_temperature)
                beta = 1.0 / max(logits.shape[-1] - 1, 1)
                reg_loss = F.smooth_l1_loss(pred_rel.float(), target_rel.float(), beta=beta)
                loss = loss + args.regression_loss_weight * reg_loss

        if train:
            assert optimizer is not None and args is not None
            lr = cosine_lr(step, total_steps, args.lr, args.warmup_ratio)
            for group in optimizer.param_groups:
                group["lr"] = lr
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            step += 1
        else:
            lr = 0.0

        batch_size = int(features.shape[0])
        if args:
            metrics = localization_metrics(
                logits.detach(),
                target_rel,
                duration,
                prediction_mode=args.metric_prediction,
                prediction_temperature=args.metric_temperature,
                argmax_weight=args.metric_argmax_weight,
                local_window=args.metric_local_window,
            )
        else:
            metrics = localization_metrics(logits.detach(), target_rel, duration)
        metrics["loss"] = loss.detach().item()
        metrics["lr"] = lr
        for key, value in metrics.items():
            totals[key] = totals.get(key, 0.0) + float(value) * batch_size
        n_examples += batch_size
        iterator.set_postfix(loss=f"{metrics['loss']:.4f}", mae=f"{metrics['mae_rel']:.4f}")

    return {key: value / max(n_examples, 1) for key, value in totals.items()}, step


def save_checkpoint(path: Path, model: torch.nn.Module, optimizer: torch.optim.Optimizer, epoch: int, metrics: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "epoch": epoch,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "metrics": metrics,
        },
        path,
    )


def main() -> None:
    args = parse_args()
    seed_everything(args.seed)
    torch.set_float32_matmul_precision("high")
    if args.feature_mode == "features" and not args.features_dir:
        raise ValueError("--features-dir is required with --feature-mode features")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "args.json").open("w", encoding="utf-8") as f:
        json.dump(vars(args), f, indent=2, sort_keys=True)

    train_loader, val_loader, feature_dim = make_loaders(args)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = build_model(
        args.model,
        input_dim=feature_dim,
        hidden_dim=args.hidden_dim,
        num_layers=args.num_layers,
        num_heads=args.num_heads,
        dropout=args.dropout,
    ).to(device)
    if args.compile:
        model = torch.compile(model)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay, betas=(0.9, 0.95), eps=1e-10)
    total_steps = max(len(train_loader) * args.epochs, 1)
    best_score = float("inf") if args.save_best_mode == "min" else -float("inf")
    step = 0
    metrics_path = output_dir / "metrics.jsonl"
    print(
        json.dumps(
            {
                "device": str(device),
                "train_batches": len(train_loader),
                "val_batches": len(val_loader),
                "feature_dim": feature_dim,
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )

    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = True
    gc.collect()

    with metrics_path.open("w", encoding="utf-8") as metrics_file:
        for epoch in range(1, args.epochs + 1):
            t0 = time.time()
            train_metrics, step = run_epoch(model, train_loader, device, optimizer, total_steps, step, args)
            with torch.no_grad():
                val_metrics, _ = run_epoch(model, val_loader, device, args=args)
            row = {
                "epoch": epoch,
                "seconds": time.time() - t0,
                "train": train_metrics,
                "val": val_metrics,
            }
            metrics_file.write(json.dumps(row) + "\n")
            metrics_file.flush()
            print(json.dumps(row, indent=2))

            save_checkpoint(output_dir / "last.pt", model, optimizer, epoch, val_metrics)
            if args.save_best_metric not in val_metrics:
                raise KeyError(f"--save-best-metric {args.save_best_metric!r} not found in validation metrics")
            current_score = float(val_metrics[args.save_best_metric])
            improved = current_score < best_score if args.save_best_mode == "min" else current_score > best_score
            if improved:
                best_score = current_score
                save_checkpoint(output_dir / "best.pt", model, optimizer, epoch, val_metrics)

    print(f"best_val_{args.save_best_metric}: {best_score:.6f}")
    if torch.cuda.is_available():
        print(f"peak_vram_mb: {torch.cuda.max_memory_allocated() / 1024 / 1024:.1f}")


if __name__ == "__main__":
    main()
