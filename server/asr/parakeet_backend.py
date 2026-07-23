"""Parakeet ASR backend — Parakeet TDT via MLX, word-by-word interims.

The low-latency backend: every chunk that doesn't close an utterance gets
a full-context re-decode of the growing in-flight audio, emitted as a
volatile ``interim`` so the transcript reveals word-by-word and the tail
self-corrects as more speech arrives. ``generate()`` is cheap enough
(<200 ms even on an 8 s window, well under the ~320 ms chunk cadence)
that this keeps up in real time. The settled ``final`` at the silence
boundary is the clean, authoritative decode.

Design notes:
  * The model is downloaded once into ``models/parakeet-tdt-0.6b-v3`` and
    loaded from that local directory, so a restart never hits the network.
  * Audio is gated through the same ``UtteranceBuffer`` VAD as the Whisper
    backend, with a slightly shorter trailing-silence window: interims
    already show the text live, so settling finals a touch sooner costs
    nothing visible and trims label latency.
  * Full-context ``generate()`` on a bounded utterance replaced an earlier
    streaming token-draft decoder that produced duplicated sentences,
    garbled half-words, and Metal watchdog GPU hangs.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from server.audio_buffer import UtteranceBuffer

log = logging.getLogger("whisper-studio")

SAMPLE_RATE = 16000

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
PARAKEET_MODEL_DIR = os.path.join(MODELS_DIR, "parakeet-tdt-0.6b-v3")
PARAKEET_REPO_ID = "mlx-community/parakeet-tdt-0.6b-v3"

# Single dedicated worker for ALL MLX work (model load/warmup + every
# session.process call). MLX evaluation streams are thread-local and a
# generate() call materializes MLX arrays, so the model must be loaded and
# every utterance decoded on the *same* thread, or MLX raises "There is no
# Stream(gpu, N) in current thread". A 1-worker pool guarantees that
# affinity. Exposed as ``executor`` per the backend contract (base.py).
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parakeet-mlx")

# Return MLX's unused buffer pool to the OS every N decodes — cheap
# long-session memory hygiene for multi-hour meetings. Counts interim
# re-decodes too (they're frequent), so the window is in "decodes".
_CLEAR_CACHE_EVERY = 50
# Don't run an interim decode until the in-flight utterance has at least
# this much audio — below ~300 ms the transcript is empty or a single
# unreliable fragment, so it's not worth the decode or the UI flicker.
_MIN_INTERIM_S = 0.3
_MIN_INTERIM_BYTES = int(_MIN_INTERIM_S * SAMPLE_RATE) * 2  # PCM16 = 2 bytes/sample
# Trailing silence that settles a final. Interims carry perceived latency,
# so this can sit slightly under the Whisper backend's 400 ms default
# while staying above normal mid-sentence pauses (~200-300 ms in English).
_TRAILING_SILENCE_MS = 350

_model = None
_get_logmel = None
_model_lock = threading.Lock()


def _ensure_parakeet_model() -> str:
    """Download the Parakeet model into models/ if not already present."""
    weight_file = os.path.join(PARAKEET_MODEL_DIR, "model.safetensors")
    if not os.path.exists(weight_file):
        from huggingface_hub import snapshot_download

        log.info("Downloading Parakeet model %s ...", PARAKEET_REPO_ID)
        snapshot_download(
            repo_id=PARAKEET_REPO_ID,
            local_dir=PARAKEET_MODEL_DIR,
            local_dir_use_symlinks=False,
        )
        log.info("Parakeet model download complete.")
    return os.path.abspath(PARAKEET_MODEL_DIR)


def _get_model():
    """Lazily load and warm up the process-wide Parakeet model."""
    global _model, _get_logmel
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            import mlx.core as mx
            import parakeet_mlx as pm
            from parakeet_mlx.audio import get_logmel

            local_dir = _ensure_parakeet_model()
            log.info("Loading Parakeet model from %s ...", local_dir)
            # An absolute local path makes from_pretrained's internal
            # hf_hub_download raise immediately (not a valid repo id) and
            # fall through to its local-directory branch — no network
            # HEAD/GET is ever attempted, even without HF_HUB_OFFLINE.
            model = pm.from_pretrained(local_dir)

            # Warm up MLX graph compilation so the first real utterance
            # doesn't eat the cold ~1.8 s compile mid-conversation.
            try:
                silent = mx.array(np.zeros(int(5.0 * SAMPLE_RATE), dtype=np.float32))
                mel = get_logmel(silent, model.preprocessor_config)
                _ = model.generate(mel)
            except Exception as e:  # warmup is best-effort
                log.debug("Parakeet warmup skipped: %s", e)

            _get_logmel = get_logmel
            _model = model
            log.info("Parakeet model loaded.")
    return _model


def preload() -> None:
    """Eagerly load the model on the dedicated MLX thread.

    Routed through ``executor`` (not the calling thread) so the model is
    loaded on the same thread that every session will later decode on.
    Safe to call from any thread at startup; blocks until loaded.
    """
    executor.submit(_get_model).result()


def is_loaded() -> bool:
    """Whether the model is currently resident in memory."""
    return _model is not None


def load() -> None:
    """Force the model into memory now (used by local mode's lazy loader).

    Must run on ``executor`` so the load lands on the MLX decode thread.
    """
    _get_model()


def unload() -> None:
    """Release the model from memory (local mode, on engine switch).

    Call AFTER any session holding a reference has been dropped, otherwise
    the weights aren't freed until that reference goes away. Runs on
    ``executor`` so the MLX free happens on the owning thread.
    """
    global _model, _get_logmel
    _model = None
    _get_logmel = None
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception as e:  # best-effort
        log.debug("Parakeet cache clear on unload failed: %s", e)
    import gc

    gc.collect()
    log.info("Parakeet model unloaded.")


class ParakeetSession:
    """One per-connection Parakeet decoder.

    Audio is gated through a per-connection ``UtteranceBuffer`` and each
    completed utterance is decoded in one shot with full-context
    ``generate()``. Not thread-safe: chunks must be fed sequentially via
    ``process()`` because the VAD buffer and decode counter are stateful.
    """

    def __init__(self) -> None:
        import mlx.core as mx

        self._mx = mx
        self._model = _get_model()
        self._get_logmel = _get_logmel
        self._buf = UtteranceBuffer(trailing_silence_ms=_TRAILING_SILENCE_MS)
        # Decodes run (interim + final), for periodic cache clearing.
        self._decodes = 0
        # Last interim text emitted, so we don't re-send an unchanged draft.
        self._last_interim = ""

    def _decode(self, pcm_bytes: bytes) -> tuple[str, np.ndarray]:
        """Full-context decode of an audio span. Returns (text, float32 audio).
        The audio is handed back so the caller can run speaker
        identification on an utterance-sized window."""
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        mel = self._get_logmel(self._mx.array(audio), self._model.preprocessor_config)
        text = self._model.generate(mel)[0].text.strip()

        self._decodes += 1
        if self._decodes % _CLEAR_CACHE_EVERY == 0:
            self._mx.clear_cache()

        return text, audio

    @staticmethod
    def _has_words(text: str) -> bool:
        # Parakeet emits nothing on silence (it's a transducer) and the VAD
        # already dropped sub-400 ms fragments, so an empty/punctuation-only
        # result is just a non-speech window — not worth emitting.
        return bool(text) and any(c.isalnum() for c in text)

    def process(self, raw_pcm: bytes) -> list[dict]:
        events: list[dict] = []
        completed = self._buf.feed(raw_pcm)
        if completed:
            for utterance_pcm in completed:
                text, audio = self._decode(utterance_pcm)
                if self._has_words(text):
                    events.append({"kind": "final", "text": text, "audio": audio})
            # An utterance just closed; the next interim starts a fresh window.
            self._last_interim = ""
            return events

        # No boundary this chunk — re-decode the growing in-flight utterance.
        pending = self._buf.pending()
        if len(pending) >= _MIN_INTERIM_BYTES:
            text, _ = self._decode(pending)
            if self._has_words(text) and text != self._last_interim:
                self._last_interim = text
                events.append({"kind": "interim", "text": text})
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        try:
            tail = self._buf.flush()
            if tail is not None:
                text, audio = self._decode(tail)
                if self._has_words(text):
                    events.append({"kind": "final", "text": text, "audio": audio})
            self._last_interim = ""
        except Exception as e:
            log.debug("Parakeet finish flush failed: %s", e)
        return events

    def close(self) -> None:
        # No stateful decoder context to release; the VAD buffer is plain RAM.
        pass


def create_session() -> ParakeetSession:
    return ParakeetSession()
