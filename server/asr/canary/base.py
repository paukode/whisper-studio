"""Vendored-support shims for the Canary module (see __init__.py provenance).

``STTOutput`` mirrors mlx_audio/stt/models/base.py; ``load_audio`` delegates
to mlx-whisper's ffmpeg-backed loader (already bundled) instead of pulling in
mlx-audio's miniaudio/soundfile stack. The backend always feeds float32
arrays, so ``load_audio`` only runs when a caller passes a file path.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class STTOutput:
    text: str
    segments: list[dict] = field(default_factory=list)
    language: str | None = None
    prompt_tokens: int = 0
    generation_tokens: int = 0
    total_tokens: int = 0
    prompt_tps: float = 0.0
    generation_tps: float = 0.0
    total_time: float = 0.0


def load_audio(path, sample_rate: int = 16000):
    import mlx.core as mx
    from mlx_whisper.audio import load_audio as whisper_load_audio

    return mx.array(whisper_load_audio(str(path), sr=sample_rate))
