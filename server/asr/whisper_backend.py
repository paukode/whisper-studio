"""Whisper ASR backend — mlx-whisper large-v3-turbo on utterance windows.

Self-contained: model paths, download, decoding parameters, and the
hallucination filters all live here. Decodes only settled utterances at
silence boundaries (no interim drafts), which is what large-v3-turbo is
good at; the latency-focused alternative is the Parakeet backend.

Model load is lazy (first session), so importing this module is cheap.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from server.audio_buffer import UtteranceBuffer
from server.infrastructure.config import get as config_get

log = logging.getLogger("whisper-studio")

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
WHISPER_MODEL_DIR = os.path.join(MODELS_DIR, "whisper-large-v3-turbo")
WHISPER_REPO_ID = "mlx-community/whisper-large-v3-turbo"

# RMS below this is treated as dead air — don't bother decoding.
ENERGY_THRESHOLD = 0.01

# Decoding is sequential per connection: utterances arrive in real time
# and decode far faster than real time (RTF well under 1), so one worker
# keeps ordering trivial and avoids interleaving MLX work across threads.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper-asr")

_model_ready = False
_model_lock = threading.Lock()
# Whether the weights are resident in mlx_whisper's in-memory cache (distinct
# from _model_ready, which only tracks on-disk presence). Local mode uses this
# to load lazily on first use and free on engine switch.
_in_memory = False

# Normalized to lowercase at definition: the match site lowercases the decoded
# text (`text.strip().lower() in WHISPER_HALLUCINATIONS`), so any uppercase
# entry below would be dead weight that could never match. Lowering the whole
# set once keeps the filters live and collapses case-duplicates automatically.
WHISPER_HALLUCINATIONS = {
    p.lower()
    for p in {
        "thank you",
        "thanks",
        "thank you.",
        "thanks.",
        "thank you for watching",
        "thanks for watching",
        "subscribe",
        "please subscribe",
        "thank you for watching.",
        "thanks for watching.",
        "like and subscribe",
        "bye",
        "bye.",
        "goodbye",
        "goodbye.",
        "you",
        "the end",
        "the end.",
        "...",
        "so",
        "uh",
        "um",
        "i'm going to go",
        "i'm going to go.",
        "let's go",
        "let's go.",
        "okay",
        "okay.",
        "alright",
        "alright.",
        "right",
        "right.",
        "yeah",
        "yeah.",
        "yes",
        "yes.",
        "no",
        "no.",
        "hmm",
        "hmm.",
        "huh",
        "huh.",
        "ah",
        "ah.",
        "oh",
        "oh.",
        "Obrigado.",
        "Obrigado",
        "see you",
        "Gracias.",
        "Gracias",
        "see you.",
        "see you later",
        "see you later.",
        "later",
        "later.see you tomorrow",
        "see you tomorrow.",
        "tomorrow",
        "tomorrow.see you soon",
        "see you soon.",
        "soon",
        "soon.",
        "I'm going to go. I'm going to go. I'm going to go.",
        "I'm ready. I'm ready. I'm ready.",
        "I'm going to go.",
        "I'm going to go",
        "I'm ready.",
        "I'm ready",
        "Obrigado por assistir!",
        "Obrigado por assistir",
        "Oh, no!",
        "Oh,",
        "Oh",
        "Oh.",
        "Oh no!",
        "Oh no",
        "Oh no.",
    }
}


def is_repetition_hallucination(text: str) -> bool:
    """Detect Whisper's looping hallucination patterns.

    Catches both long-form loops ("I love you" × 50) and the short-form
    cases the original filter missed: "Cheers cheers", "Ola ola ola",
    "I do I do I do". Three independent checks; any one is enough.
    """
    clean = re.sub(r"[^\w\s]", "", text.lower())
    words = clean.split()

    # 1. Character-level repetition: single long token with no spaces
    # (e.g. "athanathanathan...").
    if len(clean) > 40 and " " not in clean.strip():
        for n in range(2, min(12, len(clean) // 4)):
            phrase = clean[:n]
            if clean == phrase * (len(clean) // n) + phrase[: len(clean) % n]:
                return True

    # 2. Consecutive n-gram repetition: the same 1-3 word phrase three or
    # more times back-to-back ("cheers cheers cheers", "i do i do i do").
    if len(words) >= 3:
        for n in (1, 2, 3):
            if len(words) < n * 3:
                continue
            for i in range(len(words) - n * 3 + 1):
                window = words[i : i + n * 3]
                first = window[:n]
                if first == window[n : 2 * n] == window[2 * n : 3 * n]:
                    return True

    # 3. Long-form whole-text loop: a phrase that dominates the output
    # (3+ repetitions covering over 35% of the text).
    if len(words) >= 6:
        limit = min(len(words), 20)
        for start in range(limit):
            for n in range(1, 7):
                if start + n > len(words):
                    break
                phrase = " ".join(words[start : start + n])
                if not phrase:
                    continue
                count = clean.count(phrase)
                if count >= 3 and count * len(phrase) > len(clean) * 0.35:
                    return True
    return False


def _ensure_model() -> str:
    """Download the Whisper model if not already present (idempotent)."""
    global _model_ready
    if not _model_ready:
        with _model_lock:
            if not _model_ready:
                weight_file = os.path.join(WHISPER_MODEL_DIR, "weights.safetensors")
                if not os.path.exists(weight_file):
                    from huggingface_hub import snapshot_download

                    log.info("Downloading Whisper model %s ...", WHISPER_REPO_ID)
                    snapshot_download(
                        repo_id=WHISPER_REPO_ID,
                        local_dir=WHISPER_MODEL_DIR,
                        local_dir_use_symlinks=False,
                    )
                    log.info("Whisper model download complete.")
                _model_ready = True
    return os.path.abspath(WHISPER_MODEL_DIR)


def preload() -> None:
    """Eagerly load the model into memory (startup warmup, best-effort).

    Runs ``load()`` on ``executor`` so mlx-whisper's ModelHolder is populated
    on the same thread its sessions decode on — the first real utterance is
    then instant. Mirrors Parakeet's ``preload()`` so the configured engine is
    memory-resident at startup whichever backend is selected (not merely on
    disk). ``load()`` ensures the weights are downloaded first.
    """
    executor.submit(load).result()


def _model_holder():
    """mlx-whisper caches the loaded model on a class (transcribe.ModelHolder
    with class-level `model`/`model_path`), not an lru_cache — this is the
    single reference we load into and free from."""
    import importlib

    return importlib.import_module("mlx_whisper.transcribe").ModelHolder


def is_loaded() -> bool:
    """Whether the weights are resident in mlx-whisper's ModelHolder cache."""
    try:
        return _model_holder().model is not None
    except Exception:
        return _in_memory


def load() -> None:
    """Force the model into memory now (local mode's lazy loader).

    mlx-whisper loads the model inside ``transcribe`` and caches it on
    ModelHolder, so a short silent decode warms that exact cache — the first
    real utterance is then instant and the memory is committed up front. Run
    on ``executor``.
    """
    global _in_memory
    _ensure_model()
    # ~0.5s of silence: enough to trigger the model load + graph compile.
    silent = np.zeros(int(0.5 * 16000), dtype=np.float32)
    _transcribe(silent)
    _in_memory = True


def unload() -> None:
    """Release the weights from mlx-whisper's ModelHolder (local mode, switch)."""
    global _in_memory
    try:
        holder = _model_holder()
        holder.model = None
        holder.model_path = None
    except Exception as e:  # best-effort across mlx-whisper versions
        log.debug("Whisper ModelHolder clear on unload failed: %s", e)
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception as e:
        log.debug("Whisper mx cache clear on unload failed: %s", e)
    import gc

    gc.collect()
    _in_memory = False
    log.info("Whisper model unloaded.")


def _transcribe(audio_data: np.ndarray, language: str | None = None) -> str:
    """Decode one utterance with mlx-whisper.

    Decoding params suppress Whisper's well-known hallucination loops on
    silence/low-energy audio: deterministic greedy decoding, compression
    ratio and logprob rejection, real silence detection, and no prompt
    conditioning on previous (possibly hallucinated) text.
    """
    import mlx_whisper

    kwargs = {
        "path_or_hf_repo": _ensure_model(),
        "fp16": True,
        "temperature": 0.0,
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "no_speech_threshold": 0.6,
        "condition_on_previous_text": False,
    }
    if language:
        kwargs["language"] = language

    result = mlx_whisper.transcribe(audio_data, **kwargs)
    return result["text"].strip()


def _decode_utterance(utterance_pcm: bytes) -> tuple[str, np.ndarray]:
    """PCM16 utterance -> (filtered text, float32 audio)."""
    audio = np.frombuffer(utterance_pcm, dtype=np.int16).astype(np.float32) / 32768.0

    volume = np.sqrt(np.mean(audio**2))
    if volume < ENERGY_THRESHOLD:
        return "", audio

    text = ""
    try:
        text = _transcribe(audio, language=config_get("whisper_language"))
        if text.strip().lower() in WHISPER_HALLUCINATIONS or is_repetition_hallucination(text):
            text = ""
    except Exception as e:
        log.warning("Whisper transcription error: %s", e)
    return text, audio


class WhisperSession:
    """One per-connection decoder: VAD-gated utterances, final events only."""

    def __init__(self) -> None:
        self._buf = UtteranceBuffer()

    def process(self, raw_pcm: bytes) -> list[dict]:
        events: list[dict] = []
        for utterance_pcm in self._buf.feed(raw_pcm):
            text, audio = _decode_utterance(utterance_pcm)
            if text:
                events.append({"kind": "final", "text": text, "audio": audio})
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        try:
            tail = self._buf.flush()
            if tail is not None:
                text, audio = _decode_utterance(tail)
                if text:
                    events.append({"kind": "final", "text": text, "audio": audio})
        except Exception as e:
            log.debug("Whisper finish flush failed: %s", e)
        return events

    def close(self) -> None:
        pass


def create_session() -> WhisperSession:
    return WhisperSession()
