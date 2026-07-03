---
license: mit
tags:
  - temporal-action-localization
  - video-understanding
  - action-detection
  - pytorch
  - transformer
  - actionformer
  - thumos14
  - i3d
datasets:
  - thumos14
---

# ActionFormer — Temporal Action Localization on THUMOS14

End-to-end implementation of temporal action localization using the
[ActionFormer](https://arxiv.org/abs/2202.07925) transformer architecture,
trained on [THUMOS14](http://www.thumos.info/) I3D features.

## Results

> **Constrained setup:** trained on 50 videos (full THUMOS14 has ~200), single 16 GB GPU,
> `max_seq_len=1152`, 35 epochs. This checkpoint demonstrates a correct, reproducible
> pipeline under compute constraints — not a match for published numbers.
> Published ActionFormer achieves ~62% mAP with full data on an A100.

| Model | Average mAP (tIoU 0.3–0.7) |
|---|---|
| Regular weights | 4.15% |
| EMA weights (this checkpoint) | 4.38% (+0.23 pp) |

The EMA gain is small and expected in a data-limited regime.

## Files

| File | Description |
|---|---|
| `checkpoint_ema.pth.tar` | Final EMA checkpoint (use this for inference) |
| `thumos_i3d.yaml` | Model and training configuration |

## Quick Start

```bash
# 1. Clone the repo and install dependencies
git clone https://github.com/tinarawitharana/thumos14-actionformer-tal
cd thumos14-actionformer-tal
pip install -r requirements.txt

# 2. Clone the ActionFormer library (required by inference.py)
git clone https://github.com/happyharrycn/actionformer_release libs
# Then build the NMS extension per the ActionFormer README

# 3. Run inference — checkpoint auto-downloads from this HF repo
python inference.py \
    --feat    path/to/your_video_i3d_features.npy \
    --hf_repo tinanaphtali/actionformer-thumos14-i3d \
    --top_k 10 --save_fig
```

`inference.py` downloads `checkpoint_ema.pth.tar` and `thumos_i3d.yaml` from this
repo on first run and caches them locally. The feature file must be a pre-extracted
I3D `.npy` of shape `(T, 2048)` from the THUMOS14 dataset.

## Architecture

ActionFormer builds a multi-scale temporal feature pyramid over I3D features
using local windowed self-attention, then predicts action segments at each
pyramid level with a lightweight classification + DIoU regression head.
Soft-NMS post-processing handles overlapping predictions.

## Citation

```bibtex
@inproceedings{zhang2022actionformer,
  title     = {ActionFormer: Localizing Moments of Actions with Transformers},
  author    = {Zhang, Chen-Lin and Wu, Jianxin and Li, Yin},
  booktitle = {ECCV},
  year      = {2022}
}
```
