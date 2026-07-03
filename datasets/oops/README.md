# Oops! Unintentional Action Dataset

Official project page: https://oops.cs.columbia.edu/

Dataset page: https://oops.cs.columbia.edu/data/

The main package is the Oops videos plus annotations archive, listed by the
authors as about 45GB:

```bash
./download_oops.sh main
```

Additional optional packages:

```bash
./download_oops.sh captions  # natural language descriptions, about 11MB
./download_oops.sh models    # pre-trained models, about 697MB
./download_oops.sh flow      # optical-flow frames, about 1019GB
```

The dataset page states that the videos are provided for non-commercial
research and educational use and are licensed under CC BY-NC-SA 4.0.

## Official URLs

- `https://oops.cs.columbia.edu/data/video_and_anns.tar.gz`
- `https://oops.cs.columbia.edu/data/lang.tar.gz`
- `https://oops.cs.columbia.edu/data/models.tar.gz`
- `https://oops.cs.columbia.edu/data/flow.tar.gz`

## Current Environment Note

On this machine, downloads from `oops.cs.columbia.edu` are blocked by the
cluster network path:

- proxy path returns `403 Forbidden`
- direct path resolves `oops.cs.columbia.edu` to `128.59.16.27`, then times out
  during TCP connect

Run the script from a node/network with outbound access to Columbia's server, or
ask the cluster admin to allow `oops.cs.columbia.edu`.
