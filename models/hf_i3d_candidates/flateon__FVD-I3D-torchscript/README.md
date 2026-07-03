---
license: mit
---

# I3D Model for Frechet Video Distance (FVD)

This repository contains a TorchScript version of the I3D (Inflated 3D ConvNet) model, specifically for calculating Frechet Video Distance (FVD). FVD is a metric used to evaluate the quality of generated videos by comparing the statistics of generated videos with real videos.

## Overview

The I3D model is a deep neural network architecture designed for video recognition. In the context of FVD calculation, we use the I3D model to extract meaningful features from videos, which are then used to compute the distance between the feature distributions of real and generated videos.

## Installation

```bash
pip install huggingface_hub
```

## Usage

```python
import torch
from huggingface_hub import hf_hub_download

# Download the model from Hugging Face Hub
model_path = hf_hub_download(
    repo_id="flateon/FVD-I3D-torchscript",
    filename="i3d_torchscript.pt"
)

# Load the model
i3d_model = torch.jit.load(model_path)

# Example with a random video tensor
# Format: [batch_size, channels, frames, height, width]
video_tensor = torch.randn(2, 3, 16, 224, 224)

# Extract features
features = i3d_model(video_tensor, rescale=True, resize=True, return_features=True)
print(features.shape) # torch.Size([2, 400])
```

## References

- Original I3D paper: [Quo Vadis, Action Recognition? A New Model and the Kinetics Dataset](https://arxiv.org/abs/1705.07750)
- FVD metric: [Towards Accurate Generative Models of Video: A New Metric & Challenges](https://arxiv.org/abs/1812.01717)
