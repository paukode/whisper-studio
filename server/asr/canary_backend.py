"""Canary ASR backend — NVIDIA Canary-1B-v2 via vendored MLX code.

Transcribes 25 European languages AND translates any of them to English with
a real speech-translation head (unlike whisper-large-v3-turbo, whose
translate task is silently ignored). Decodes settled utterances at silence
boundaries like the Whisper backend; steady-state decode is ~0.4 s per
utterance on Apple Silicon.

One Canary limitation shapes this module: the model has NO language
auto-detection — every decode needs an explicit ``source_lang``. The session
language comes from the ``whisper_language`` allowlist (first entry), so
Canary suits single-language sessions; mixed-language meetings are Whisper's
territory.

Model load is lazy (first session); importing this module is cheap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from server.audio_buffer import UtteranceBuffer
from server.infrastructure.paths import models_root

log = logging.getLogger("whisper-studio")

MODELS_DIR = models_root()
CANARY_MODEL_DIR = os.path.join(MODELS_DIR, "canary-1b-v2")
CANARY_REPO_ID = "qfuxa/canary-mlx"

# Languages Canary-1B-v2 transcribes (and translates to/from English).
CANARY_LANGUAGES = {
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu",
    "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru",
    "uk",
}  # fmt: skip

# Same rationale as the Whisper backend: RMS below this is dead air.
ENERGY_THRESHOLD = 0.01

# Single worker: MLX evaluation streams are thread-local, so the model must
# load and decode on one thread (same constraint as the Parakeet backend).
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="canary-asr")

_model = None
_model_lock = threading.Lock()


def _ensure_model() -> str:
    """Download the Canary MLX weights if not already present (idempotent)."""
    weight_file = os.path.join(CANARY_MODEL_DIR, "model.safetensors")
    if not os.path.exists(weight_file):
        from huggingface_hub import snapshot_download

        log.info("Downloading Canary model %s ...", CANARY_REPO_ID)
        snapshot_download(
            repo_id=CANARY_REPO_ID,
            local_dir=CANARY_MODEL_DIR,
            local_dir_use_symlinks=False,
        )
        log.info("Canary model download complete.")
    return os.path.abspath(CANARY_MODEL_DIR)


def is_loaded() -> bool:
    return _model is not None


def load() -> None:
    """Load the model into memory (mirrors mlx-audio's base_load_model steps,
    minus its quantization/remapping branches the fp16 repo never hits). Run
    on ``executor``."""
    global _model
    if _model is not None:
        return
    with _model_lock:
        if _model is not None:
            return
        from server.asr.canary import Model, ModelConfig

        model_dir = Path(_ensure_model())
        import mlx.core as mx

        log.info("Loading Canary model from %s ...", model_dir)
        config = json.loads((model_dir / "config.json").read_text())
        config["model_path"] = str(model_dir)
        model = Model(ModelConfig.from_dict(config))
        weights = mx.load(str(model_dir / "model.safetensors"))
        if hasattr(model, "sanitize"):
            weights = model.sanitize(weights)
        model.load_weights(list(weights.items()), strict=False)
        mx.eval(model.parameters())
        model.eval()
        # Loads the SentencePiece tokenizer from the model dir.
        _model = Model.post_load_hook(model, model_dir)
        log.info("Canary model loaded.")


def preload() -> None:
    """Eager startup warmup on the decode thread (best-effort)."""
    executor.submit(load).result()


def unload() -> None:
    """Release the weights (local mode, engine switch)."""
    global _model
    _model = None
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception as e:
        log.debug("Canary mx cache clear on unload failed: %s", e)
    import gc

    gc.collect()
    log.info("Canary model unloaded.")


def _session_language() -> str:
    """The session's source language.

    Canary cannot auto-detect, so this is the first Canary-supported code in
    the ``whisper_language`` allowlist, falling back to English. Unsupported
    codes are logged once per decode batch rather than crashing the stream.
    """
    from server.asr.whisper_backend import _parse_languages
    from server.infrastructure.config import get as config_get

    for code in _parse_languages(config_get("whisper_language")):
        if code in CANARY_LANGUAGES:
            return code
        log.warning("Canary: unsupported language %r in whisper_language, skipping", code)
    return "en"


def _generate(audio: np.ndarray, source_lang: str, target_lang: str) -> str:
    load()
    result = _model.generate(audio, source_lang=source_lang, target_lang=target_lang)
    return (result.text or "").strip()


def translate_utterance(
    audio_data: np.ndarray, language: str | None = None, target: str = "en"
) -> str:
    """Translation of one utterance via Canary's native AST head.

    Canary translates bidirectionally with ENGLISH AS THE HUB: any of its 25
    languages → English, and English → any of them — never X → Y with both
    non-English (the orchestrator's resolver enforces that). Same
    orchestration contract as the Whisper backend's translate pass: called
    on ``executor`` after the transcript final was emitted.
    """
    from server.asr.whisper_backend import _is_junk

    source = language if language in CANARY_LANGUAGES else _session_language()
    if target not in CANARY_LANGUAGES or (source != "en" and target != "en"):
        log.warning("Canary: unsupported translation pair %s->%s, skipped", source, target)
        return ""
    text = ""
    try:
        text = _generate(audio_data, source_lang=source, target_lang=target)
        if text and _is_junk(text):
            text = ""
    except Exception as e:
        log.warning("Canary translation error: %s", e)
    return text


def _decode_utterance(utterance_pcm: bytes) -> tuple[str, np.ndarray, str | None]:
    """PCM16 utterance -> (filtered text, float32 audio, source language)."""
    from server.asr.whisper_backend import _is_junk

    audio = np.frombuffer(utterance_pcm, dtype=np.int16).astype(np.float32) / 32768.0

    volume = np.sqrt(np.mean(audio**2))
    if volume < ENERGY_THRESHOLD:
        log.debug("Canary: utterance below energy threshold (rms=%.4f), skipped", volume)
        return "", audio, None

    language = _session_language()
    text = ""
    try:
        text = _generate(audio, source_lang=language, target_lang=language)
        if text and _is_junk(text):
            log.debug("Canary: hallucination filter dropped %r", text[:80])
            text = ""
    except Exception as e:
        log.warning("Canary transcription error: %s", e)
    return text, audio, language


class CanarySession:
    """One per-connection decoder: VAD-gated utterances, final events only."""

    def __init__(self) -> None:
        self._buf = UtteranceBuffer()

    def process(self, raw_pcm: bytes) -> list[dict]:
        events: list[dict] = []
        for utterance_pcm in self._buf.feed(raw_pcm):
            text, audio, language = _decode_utterance(utterance_pcm)
            if text:
                events.append({"kind": "final", "text": text, "audio": audio, "language": language})
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        try:
            tail = self._buf.flush()
            if tail is not None:
                text, audio, language = _decode_utterance(tail)
                if text:
                    events.append(
                        {"kind": "final", "text": text, "audio": audio, "language": language}
                    )
        except Exception as e:
            log.debug("Canary finish flush failed: %s", e)
        return events

    def close(self) -> None:
        pass


def create_session() -> CanarySession:
    return CanarySession()
