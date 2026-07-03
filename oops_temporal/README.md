# Oops Temporal Localization

This package builds a temporal detector for the Oops unintentional-action
localization task.

The training target is a distribution over normalized time bins. The model
predicts one logit per bin; evaluation reports relative-time MAE, seconds MAE,
and tolerance accuracy at 5%, 10%, and 20% of video duration.

## Current Data

Annotations are extracted at:

```text
datasets/oops/annotations
```

The downloaded raw release archive is:

```text
datasets/oops/hf_pengxiang/video_and_anns.tar.gz
```

## Smoke Baseline

Run the annotation-only time-prior baseline:

```bash
bash scripts/train_oops_time_baseline.sh
```

This validates the label pipeline, loss, metrics, checkpointing, and experiment
logging. It does not use video content.

Observed on the current CPU environment:

| Method | Val MAE rel | Val MAE sec | Acc@0.10 |
| --- | ---: | ---: | ---: |
| predict 0.5 | 0.203696 | 2.098174 | 0.291791 |
| train-set mean | 0.198158 | 1.991303 | 0.313930 |
| train-set median | 0.197823 | 1.971385 | 0.317413 |
| time MLP baseline | 0.197856 | 1.976442 | 0.314428 |
| metadata MLP baseline | 0.196365 | 1.898271 | 0.327114 |

This is a lower bound and a sanity check. Meaningful improvement requires video
features.

## Feature-Based Training

For a real temporal detector, create per-video features as `.pt` or `.npy` files:

```text
datasets/oops/features/<safe_video_id>.pt
```

Each feature file must contain a float array shaped `[time, dim]`. The loader
also accepts the original video id as the filename when it is filesystem-safe.

Extract the raw videos from the downloaded HF archive:

```bash
bash scripts/extract_oops_videos.sh
```

Then extract frozen I3D features:

```bash
PYTHON_BIN=/vast/users/guangyi.chen/anaconda3/envs/simulation-hessian2-rocm/bin/python \
  sbatch scripts/submit_oops_i3d_features.sbatch
```

The default extractor uses PyTorchVideo `i3d_r50` pretrained on Kinetics and
saves `datasets/oops/features/i3d_r50_8fps16_stride4/<safe_video_id>.pt`.
Each file contains `features`, `times_sec`, the source video path, and the
feature extraction metadata.

If the official PyTorchVideo weight URL is blocked, place
`I3D_8x8_R50.pyth` locally and pass:

```bash
CHECKPOINT_PATH=/path/to/I3D_8x8_R50.pyth \
  PYTHON_BIN=/vast/users/guangyi.chen/anaconda3/envs/simulation-hessian2-rocm/bin/python \
  sbatch scripts/submit_oops_i3d_features.sbatch
```

The extractor also has an explicit `--backbone torchvision_r3d18` fallback for
testing the decode and training path; this is not I3D.

On the current 8-GPU allocation, run sharded extraction with:

```bash
CHECKPOINT_PATH=/path/to/I3D_8x8_R50.pyth \
  bash scripts/run_oops_i3d_features_8gpu.sh
```

Then train:

```bash
FEATURES_DIR=datasets/oops/features/i3d_r50_8fps16_stride4 \
  sbatch scripts/submit_oops_temporal_features.sbatch
```

## Improved I3D Run

The strongest existing run in this workspace is
`runs/oops_temporal/inception_i3d_fvd400_8fps16_stride4_transformer`, with
validation `acc_010 ~= 0.478` and `mae_rel ~= 0.171`. To target another
accuracy gain over the baseline MLPs, use the plus run:

```bash
sbatch scripts/submit_oops_temporal_i3d_plus.sbatch
```

This keeps the frozen I3D features, appends explicit time/duration features,
adds a hard-bin cross entropy and soft relative-time regression auxiliary loss,
uses blend decoding for metrics, and saves `best.pt` by maximum validation
`acc_010`.

Recommended performance path:

1. Extract 8-16 frame/second clip features with a pretrained video backbone
   such as VideoMAE, TimeSformer, InternVideo, or CLIP frame features plus
   temporal pooling.
2. Train the provided Transformer temporal head with `NUM_BINS=128`.
3. Tune LR first (`1e-4`, `3e-4`, `1e-3`), then label sigma and hidden size.
4. Add hard-negative videos where annotators mark no transition, using a
   background class or focal loss.
5. Report validation MAE and tolerance accuracy from `metrics.jsonl`.
