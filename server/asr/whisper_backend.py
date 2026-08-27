"""Whisper ASR backend — mlx-whisper large-v3 on utterance windows.

Self-contained: model paths, download, decoding parameters, and the
hallucination filters all live here. Decodes only settled utterances at
silence boundaries (no interim drafts); the latency-focused alternative is
the Parakeet backend. Runs the FULL large-v3 checkpoint: best multilingual
accuracy (translation is Canary's or Apple's
job — see server/asr/canary_backend.py).

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
from server.infrastructure.paths import models_root

log = logging.getLogger("whisper-studio")

MODELS_DIR = models_root()
WHISPER_MODEL_DIR = os.path.join(MODELS_DIR, "whisper-large-v3")
WHISPER_REPO_ID = "mlx-community/whisper-large-v3-mlx"
# This MLX repo ships npz weights, not safetensors.
WHISPER_SENTINEL = "weights.npz"


# RMS below this is treated as dead air — don't bother decoding.
ENERGY_THRESHOLD = 0.01

# Decoding is sequential per connection: utterances arrive in real time
# and decode far faster than real time (RTF well under 1), so one worker
# keeps ordering trivial and avoids interleaving MLX work across threads.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper-asr")

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
    with _model_lock:
        if not os.path.exists(os.path.join(WHISPER_MODEL_DIR, WHISPER_SENTINEL)):
            from huggingface_hub import snapshot_download

            log.info("Downloading Whisper model %s ...", WHISPER_REPO_ID)
            snapshot_download(
                repo_id=WHISPER_REPO_ID, local_dir=WHISPER_MODEL_DIR, local_dir_use_symlinks=False
            )
            log.info("Whisper model download complete.")
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
        holder = _model_holder()
        return holder.model is not None and holder.model_path == os.path.abspath(WHISPER_MODEL_DIR)
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


def _parse_languages(raw: str | None) -> list[str]:
    """``whisper_language`` config values: '' -> auto-detect, 'pl' -> pinned,
    'pl,en' -> constrain per-utterance detection to that allowlist."""
    if not raw:
        return []
    return [code.strip().lower() for code in str(raw).split(",") if code.strip()]


def _configured_languages() -> list[str]:
    """The validated ``whisper_language`` allowlist (unknown codes dropped)."""
    langs = _parse_languages(config_get("whisper_language"))
    if not langs:
        return []
    try:
        from mlx_whisper.tokenizer import LANGUAGES

        known = set(LANGUAGES)
    except Exception:
        return langs
    bad = [code for code in langs if code not in known]
    if bad:
        log.warning("whisper_language: ignoring unknown code(s): %s", ", ".join(bad))
    return [code for code in langs if code in known]


def _pick_language(probs: dict, allowed: list[str]) -> str:
    """Argmax over the allowlist only, ignoring every other language."""
    return max(allowed, key=lambda code: probs.get(code, 0.0))


def _detect_language(audio_data: np.ndarray, allowed: list[str]) -> str:
    """Constrained language ID for one utterance.

    Whisper's built-in detection argmaxes over all 99 languages, and on
    short accented utterances it routinely lands on a neighbor language —
    which either garbles the text or silently *translates* it (a Polish
    utterance decoded with an ``en`` token comes out as English). Detecting
    ourselves and argmaxing over the user's allowlist removes both failure
    modes. Cost-neutral: passing ``language`` to transcribe skips its own
    internal detection pass.
    """
    import mlx.core as mx
    from mlx_whisper import audio as whisper_audio

    model = _model_holder().get_model(_ensure_model(), mx.float16)
    mel = whisper_audio.log_mel_spectrogram(
        audio_data, n_mels=model.dims.n_mels, padding=whisper_audio.N_SAMPLES
    )
    mel = whisper_audio.pad_or_trim(mel, whisper_audio.N_FRAMES, axis=-2).astype(mx.float16)
    _, probs = model.detect_language(mel)
    if isinstance(probs, list):
        probs = probs[0]
    return _pick_language(probs, allowed)


def _transcribe(
    audio_data: np.ndarray,
    language: str | None = None,
    relaxed: bool = False,
) -> tuple[str, str | None]:
    """Decode one utterance with mlx-whisper -> (text, decoded language).

    Strict (default) decoding params suppress Whisper's well-known
    hallucination loops on silence/low-energy audio: deterministic greedy
    decoding, compression ratio and logprob rejection, real silence
    detection, and no prompt conditioning on previous (possibly
    hallucinated) text.

    ``relaxed=True`` is the rescue pass for utterances the strict pass
    dropped entirely: a small temperature ladder and no confidence
    rejection, with the compression-ratio guard and the caller's
    hallucination filters still standing between it and the transcript.
    """
    import mlx_whisper

    kwargs = {
        "path_or_hf_repo": _ensure_model(),
        "fp16": True,
        "compression_ratio_threshold": 2.4,
        "condition_on_previous_text": False,
    }
    if relaxed:
        kwargs.update(temperature=(0.0, 0.2, 0.4), logprob_threshold=None, no_speech_threshold=None)
    else:
        kwargs.update(temperature=0.0, logprob_threshold=-1.0, no_speech_threshold=0.6)
    if language:
        kwargs["language"] = language

    result = mlx_whisper.transcribe(audio_data, **kwargs)
    return result["text"].strip(), result.get("language") or language


def _is_junk(text: str) -> bool:
    return text.strip().lower() in WHISPER_HALLUCINATIONS or is_repetition_hallucination(text)


def _decode_utterance(utterance_pcm: bytes) -> tuple[str, np.ndarray, str | None]:
    """PCM16 utterance -> (filtered text, float32 audio, decoded language)."""
    audio = np.frombuffer(utterance_pcm, dtype=np.int16).astype(np.float32) / 32768.0

    volume = np.sqrt(np.mean(audio**2))
    if volume < ENERGY_THRESHOLD:
        log.debug("Whisper: utterance below energy threshold (rms=%.4f), skipped", volume)
        return "", audio, None

    text = ""
    language = None
    try:
        langs = _configured_languages()
        language = langs[0] if len(langs) == 1 else None
        if len(langs) > 1:
            language = _detect_language(audio, langs)
        text, language = _transcribe(audio, language=language)
        if text and _is_junk(text):
            log.debug("Whisper: hallucination filter dropped %r", text[:80])
            text = ""
        if not text:
            # The strict pass drops low-confidence segments wholesale
            # (no_speech + logprob rejection). On accented or overlapped
            # speech that silently eats real utterances, so retry once
            # relaxed — the filters above still guard the result.
            text, language = _transcribe(audio, language=language, relaxed=True)
            if text and _is_junk(text):
                log.debug("Whisper: hallucination filter dropped rescue %r", text[:80])
                text = ""
            if text:
                log.info(
                    "Whisper: relaxed retry rescued a suppressed utterance (lang=%s, %d chars)",
                    language or "auto",
                    len(text),
                )
    except Exception as e:
        log.warning("Whisper transcription error: %s", e)
    return text, audio, language


class WhisperSession:
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
            log.debug("Whisper finish flush failed: %s", e)
        return events

    def close(self) -> None:
        pass


def create_session() -> WhisperSession:
    return WhisperSession()
