"""Canary translator: NVIDIA Canary-1B-v2 via vendored MLX code.

Canary is the app's universal model translator. It never transcribes: the
transcript comes from Whisper or Parakeet, and Canary's speech-translation
head turns the same audio into the translation line, whichever engine
transcribed it. It translates between its 25 European languages and
English, with English always on one side of the pair.

Canary has no language head, so every decode names a source language. A
line from Whisper carries Whisper's language; a line from Parakeet carries
none, and a small on-CPU classifier (server/asr/lid.py) picks it among
Canary's languages.

Model load is lazy (first translation); importing this module is cheap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from server.infrastructure.paths import models_root

log = logging.getLogger("whisper-studio")

MODELS_DIR = models_root()
CANARY_MODEL_DIR = os.path.join(MODELS_DIR, "canary-1b-v2")
SAMPLE_RATE = 16000

CANARY_REPO_ID = "qfuxa/canary-mlx"

# Languages Canary-1B-v2 translates to and from English.
CANARY_LANGUAGES = {
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu",
    "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru",
    "uk",
}  # fmt: skip

# Where language ID looks for the source of a line whose engine named none:
# every Canary language, English first (ties go to the earlier one).
_SOURCE_CANDIDATES = ("en", *sorted(CANARY_LANGUAGES - {"en"}))

# Single worker: MLX evaluation streams are thread-local, so the model must
# load and decode on one thread.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="canary-translate")

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


def unload() -> None:
    """Release the weights."""
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


def _lid(audio: np.ndarray, candidates: tuple[str, ...]) -> tuple[str | None, float]:
    """Language ID over ``candidates`` (resolved at call time, so tests and
    callers see a patched ``lid.detect``)."""
    from server.asr import lid

    return lid.detect(audio, candidates)


def _generate(audio: np.ndarray, source_lang: str, target_lang: str) -> str:
    load()
    result = _model.generate(audio, source_lang=source_lang, target_lang=target_lang)
    return (result.text or "").strip()


def translate_utterance(
    audio_data: np.ndarray, language: str | None = None, target: str = "en"
) -> str:
    """Translation of one utterance via Canary's native AST head. Run on
    ``executor``.

    ``language`` is the transcribing engine's language when it has one
    (Whisper). A line without one (Parakeet) is detected here among all of
    Canary's languages: the target, not the pick, decides the output
    language, so a wrong pick can only skip or weaken one line. When the
    classifier is down the source is English.

    Canary translates bidirectionally with ENGLISH AS THE HUB: any of its 25
    languages to English, and English to any of them, never X to Y with both
    non-English. An unsupported pair (or same-language input) returns "" so
    the client's pending slot clears without a bogus line.
    """
    from server.asr.whisper_backend import is_repetition_hallucination

    source = language
    if source not in CANARY_LANGUAGES:
        detected, share = _lid(audio_data, _SOURCE_CANDIDATES)
        source = detected or _SOURCE_CANDIDATES[0]
        log.debug("Canary translation source: %s (lid %s %.2f)", source, detected, share)
    if source == target:
        return ""
    if target not in CANARY_LANGUAGES or (source != "en" and target != "en"):
        log.warning("Canary: unsupported translation pair %s->%s, skipped", source, target)
        return ""
    text = ""
    try:
        text = _generate(audio_data, source_lang=source, target_lang=target)
        # Only the loop check: the source already passed its own engine's
        # transcript filter, so a short phrase here ("Thank you." for
        # "Dziekuje.") is a real translation, not a silence hallucination.
        if text and is_repetition_hallucination(text):
            text = ""
    except Exception as e:
        log.warning("Canary translation error: %s", e)
    return text
