"""NVIDIA Canary-1B-v2 on MLX — vendored from mlx-audio (MIT).

Provenance: https://github.com/Blaizzy/mlx-audio v0.5.0,
``mlx_audio/stt/models/canary`` plus the parakeet conformer/audio modules and
``mlx_audio/dsp.py`` they depend on. Vendored rather than depended on because
mlx-audio's install requirements include sounddevice/miniaudio (audio device
I/O) and transformers>=5.14, none of which this headless decode path needs.
Each file carries its upstream path; diffs against upstream should be only
the headers and package-relative import rewrites.

The model weights (qfuxa/canary-mlx) are CC-BY-4.0 from NVIDIA's
nvidia/canary-1b-v2 release.
"""

from .canary import Model, ModelConfig

__all__ = ["Model", "ModelConfig"]
