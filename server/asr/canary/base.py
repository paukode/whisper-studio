"""Vendored-support shims for the Canary module (see __init__.py provenance).

``STTOutput`` mirrors mlx_audio/stt/models/base.py. The backend always feeds
float32 arrays, so no audio file loader is vendored.
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
