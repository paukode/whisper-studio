"""Canary ASR backend — NVIDIA Canary-1B-v2 via vendored MLX code.

Transcribes 25 European languages AND translates any of them to English with
a real speech-translation head (unlike Whisper checkpoints without one, whose
translate task is silently ignored). Emits a live word-by-word draft like
the Parakeet backend — the in-flight utterance is re-decoded on every audio
chunk (steady-state decode ~0.4 s, under the ~1 s chunk cadence) — with the
clean, settled final at each silence boundary.

One Canary limitation shapes this module: the model has NO language
head — every decode needs an explicit ``source_lang``. A small on-CPU
language-ID classifier (server/asr/lid.py) supplies it per utterance,
constrained to the ``whisper_language`` allowlist when one is set, so Canary
handles mixed-language meetings like Whisper does. A single-entry allowlist
skips detection entirely (pinned language, zero overhead).

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
SAMPLE_RATE = 16000

# Don't run an interim decode until the in-flight utterance carries at least
# this much audio — below ~0.5 s the draft is empty or one unreliable
# fragment, not worth the decode or the UI flicker (same idea as the
# Parakeet backend).
_MIN_INTERIM_SECONDS = 0.5
_MIN_INTERIM_BYTES = int(_MIN_INTERIM_SECONDS * SAMPLE_RATE) * 2  # PCM16
CANARY_REPO_ID = "qfuxa/canary-mlx"

# Languages Canary-1B-v2 transcribes (and translates to/from English).
CANARY_LANGUAGES = {
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu",
    "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru",
    "uk",
}  # fmt: skip

# Same rationale as the Whisper backend: RMS below this is dead air. Set at a
# true-silence floor, NOT a "quiet speech" level — the VAD already guarantees
# speech-like content, and a 0.01 gate silently ate every utterance from a
# quiet mic (proven live: RMS 0.006 speech transcribed perfectly once past it).
ENERGY_THRESHOLD = 0.002

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


def _allowed_languages() -> list[str]:
    """Canary-supported codes from the ``whisper_language`` allowlist."""
    from server.asr.whisper_backend import _parse_languages
    from server.infrastructure.config import get as config_get

    allowed = []
    for code in _parse_languages(config_get("whisper_language")):
        if code in CANARY_LANGUAGES:
            allowed.append(code)
        else:
            log.warning("Canary: unsupported language %r in whisper_language, skipping", code)
    return allowed


# Sticky-detection tuning. VoxLingua on SHORT, noisy mic utterances misroutes
# hard (observed live: Polish speech tagged sl/nl/mt/ru), so a detection only
# gets to SET or SWITCH the session language when the clip is long enough to
# carry a real language signature and the winner clearly beats the other
# candidates; anything weaker inherits the previous utterance's language.
_LID_MIN_SECONDS = 1.5
_LID_MIN_CONFIDENCE = 0.6


def _utterance_language(audio: np.ndarray, previous: str | None = None) -> str:
    """Source language for one utterance.

    A single-entry allowlist pins the language (no detection cost). Otherwise
    the LID classifier detects it, constrained to the allowlist when one is
    set or to Canary's 25 languages when not — but only a CONFIDENT detection
    on a long-enough clip is believed outright; weak ones fall back to
    ``previous`` (the session's sticky language), then the allowlist head,
    then English.
    """
    allowed = _allowed_languages()
    if len(allowed) == 1:
        return allowed[0]
    from server.asr import lid

    detected, confidence = lid.detect(audio, allowed or CANARY_LANGUAGES)
    duration = len(audio) / SAMPLE_RATE
    if (
        detected in CANARY_LANGUAGES
        and confidence >= _LID_MIN_CONFIDENCE
        and duration >= _LID_MIN_SECONDS
    ):
        return detected
    if previous in CANARY_LANGUAGES:
        return previous
    if detected in CANARY_LANGUAGES:
        return detected
    return allowed[0] if allowed else "en"


def _generate(audio: np.ndarray, source_lang: str, target_lang: str) -> str:
    load()
    result = _model.generate(audio, source_lang=source_lang, target_lang=target_lang)
    return (result.text or "").strip()


def translate_utterance(
    audio_data: np.ndarray, language: str | None = None, target: str = "en"
) -> str:
    """Translation of one utterance via Canary's native AST head.

    Canary is the app's universal model translator — this runs on Canary's
    own executor regardless of which engine transcribed the audio, so
    Whisper and Parakeet sessions can translate through it too. ``language``
    is the transcribing engine's language ID when it has one; utterances
    from engines without language ID (Parakeet) are detected here.

    Canary translates bidirectionally with ENGLISH AS THE HUB: any of its 25
    languages → English, and English → any of them — never X → Y with both
    non-English. An unsupported pair (or same-language input) returns "" so
    the client's pending slot clears without a bogus line.
    """
    from server.asr.whisper_backend import _is_junk

    source = language if language in CANARY_LANGUAGES else _utterance_language(audio_data)
    # (translation keeps stateless detection: the transcribing engine's own
    # language ID rides on the event for every engine that has one)
    if source == target:
        return ""
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


def _decode_utterance(
    utterance_pcm: bytes, previous: str | None = None
) -> tuple[str, np.ndarray, str | None]:
    """PCM16 utterance -> (filtered text, float32 audio, source language)."""
    from server.asr.whisper_backend import _is_junk

    audio = np.frombuffer(utterance_pcm, dtype=np.int16).astype(np.float32) / 32768.0

    volume = np.sqrt(np.mean(audio**2))
    if volume < ENERGY_THRESHOLD:
        log.debug("Canary: utterance below energy threshold (rms=%.4f), skipped", volume)
        return "", audio, None

    language = _utterance_language(audio, previous)
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
    """One per-connection decoder: live interim drafts plus settled finals.

    Tracks the session's sticky language: short or ambiguous utterances
    inherit it instead of trusting a shaky per-clip detection (a meeting
    rarely changes language mid-sentence)."""

    def __init__(self) -> None:
        self._buf = UtteranceBuffer()
        self._language: str | None = None
        self._last_interim = ""

    def _decode(self, utterance_pcm: bytes) -> tuple[str, np.ndarray, str | None]:
        text, audio, language = _decode_utterance(utterance_pcm, self._language)
        if language:
            self._language = language
        return text, audio, language

    def process(self, raw_pcm: bytes) -> list[dict]:
        events: list[dict] = []
        completed = self._buf.feed(raw_pcm)
        if completed:
            for utterance_pcm in completed:
                text, audio, language = self._decode(utterance_pcm)
                if text:
                    events.append(
                        {"kind": "final", "text": text, "audio": audio, "language": language}
                    )
            # An utterance just closed; the next interim starts a fresh window.
            self._last_interim = ""
            return events

        # No boundary this chunk — re-decode the growing in-flight utterance
        # as a volatile draft (mirrors the Parakeet backend). The draft reuses
        # the session's sticky language when one is settled; language
        # detection itself still only updates on finals, so a half-word
        # fragment can't flip it.
        pending = self._buf.pending()
        if len(pending) >= _MIN_INTERIM_BYTES:
            audio = np.frombuffer(pending, dtype=np.int16).astype(np.float32) / 32768.0
            language = self._language or _utterance_language(audio)
            try:
                text = _generate(audio, source_lang=language, target_lang=language)
            except Exception as e:
                log.debug("Canary interim decode failed: %s", e)
                text = ""
            if text and text != self._last_interim:
                self._last_interim = text
                events.append({"kind": "interim", "text": text})
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        try:
            tail = self._buf.flush()
            if tail is not None:
                text, audio, language = self._decode(tail)
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
