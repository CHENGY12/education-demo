from __future__ import annotations

import torch


def _bin_positions(logits: torch.Tensor) -> torch.Tensor:
    return torch.linspace(0.0, 1.0, logits.shape[-1], device=logits.device)


def prediction_from_logits(
    logits: torch.Tensor,
    mode: str = "argmax",
    temperature: float = 1.0,
    argmax_weight: float = 0.5,
    local_window: int = 8,
) -> torch.Tensor:
    bins = _bin_positions(logits)
    idx = logits.argmax(dim=-1).float()
    denom = max(logits.shape[-1] - 1, 1)
    argmax_pred = idx / denom

    if mode == "argmax":
        return argmax_pred
    if mode == "soft":
        return soft_prediction_from_logits(logits, temperature=temperature)
    if mode == "blend":
        weight = float(torch.tensor(argmax_weight).clamp(0.0, 1.0).item())
        soft_pred = soft_prediction_from_logits(logits, temperature=temperature)
        return weight * argmax_pred + (1.0 - weight) * soft_pred
    if mode == "local":
        peak = logits.argmax(dim=-1)
        pos = torch.arange(logits.shape[-1], device=logits.device).unsqueeze(0)
        mask = (pos - peak.unsqueeze(1)).abs() <= max(int(local_window), 0)
        masked_logits = logits.masked_fill(~mask, torch.finfo(logits.dtype).min)
        prob = torch.softmax(masked_logits / max(float(temperature), 1e-6), dim=-1)
        return (prob * bins).sum(dim=-1)
    raise ValueError(f"unknown prediction mode: {mode}")


def soft_prediction_from_logits(logits: torch.Tensor, temperature: float = 1.0) -> torch.Tensor:
    prob = torch.softmax(logits / max(float(temperature), 1e-6), dim=-1)
    bins = _bin_positions(logits)
    return (prob * bins).sum(dim=-1)


def localization_metrics(
    logits: torch.Tensor,
    target_rel: torch.Tensor,
    duration: torch.Tensor,
    prediction_mode: str = "argmax",
    prediction_temperature: float = 1.0,
    argmax_weight: float = 0.5,
    local_window: int = 8,
) -> dict[str, float]:
    pred_rel = prediction_from_logits(
        logits,
        mode=prediction_mode,
        temperature=prediction_temperature,
        argmax_weight=argmax_weight,
        local_window=local_window,
    )
    abs_err_rel = (pred_rel - target_rel).abs()
    abs_err_sec = abs_err_rel * duration
    return {
        "mae_rel": abs_err_rel.mean().item(),
        "median_ae_rel": abs_err_rel.median().item(),
        "mae_sec": abs_err_sec.mean().item(),
        "acc_005": (abs_err_rel <= 0.05).float().mean().item(),
        "acc_010": (abs_err_rel <= 0.10).float().mean().item(),
        "acc_020": (abs_err_rel <= 0.20).float().mean().item(),
    }
