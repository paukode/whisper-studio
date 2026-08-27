"""Spoken-language identification — VoxLingua107 ECAPA via speechbrain.

Canary has no language head (its decoder needs an explicit source-language
token), so this small classifier supplies it: ~80 MB, 107 languages, and
~40-60 ms per utterance on CPU (probed on-device). CPU on purpose — MPS is
contended by the ASR models and the index embedder, and at this size CPU is
faster than paying the transfer.

``detect`` optionally constrains the argmax to an allowlist (the user's
``whisper_language`` list intersected with what the caller supports), the
same trick the Whisper backend uses for its own detection: the model still
scores all 107 languages, we just never pick one the session can't use.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Collection
from functools import lru_cache

import numpy as np

from server.infrastructure.paths import models_root

log = logging.getLogger("whisper-studio")

MODELS_DIR = models_root()
LID_MODEL_DIR = os.path.join(MODELS_DIR, "lang-id-voxlingua107-ecapa")
LID_REPO_ID = "speechbrain/lang-id-voxlingua107-ecapa"

_classifier = None
_lock = threading.Lock()


def _ensure_model() -> str:
    """Download the LID model into models/ if absent (idempotent)."""
    hyperparams = os.path.join(LID_MODEL_DIR, "hyperparams.yaml")
    if not os.path.exists(hyperparams):
        from huggingface_hub import snapshot_download

        log.info("Downloading language-ID model %s ...", LID_REPO_ID)
        snapshot_download(
            repo_id=LID_REPO_ID,
            local_dir=LID_MODEL_DIR,
            local_dir_use_symlinks=False,
        )
        log.info("Language-ID model download complete.")
    return os.path.abspath(LID_MODEL_DIR)


def _get_classifier():
    global _classifier
    if _classifier is not None:
        return _classifier
    with _lock:
        if _classifier is None:
            _ensure_model()
            log.info("Loading language-ID model...")
            from speechbrain.inference.classifiers import EncoderClassifier

            _classifier = EncoderClassifier.from_hparams(
                source=os.path.abspath(LID_MODEL_DIR),
                savedir=os.path.abspath(LID_MODEL_DIR),
                run_opts={"device": "cpu"},
            )
            log.info("Language-ID model loaded.")
    return _classifier


@lru_cache(maxsize=1)
def _codes() -> tuple[str, ...]:
    """ISO codes in classifier-output order (labels look like 'pl: Polish')."""
    ind2lab = _get_classifier().hparams.label_encoder.ind2lab
    return tuple(ind2lab[i].split(":")[0].strip() for i in range(len(ind2lab)))


def pick_language(probs, codes: tuple[str, ...], allowed: Collection[str] | None) -> str:
    """Argmax over ``codes``, restricted to ``allowed`` when given (pure,
    unit-testable). ``probs`` is indexable per class."""
    candidates = [i for i, c in enumerate(codes) if not allowed or c in allowed]
    if not candidates:
        candidates = range(len(codes))
    best = max(candidates, key=lambda i: float(probs[i]))
    return codes[best]


def detect(audio: np.ndarray, allowed: Collection[str] | None = None) -> str | None:
    """Language code for one float32 mono 16 kHz utterance, or None on failure.

    Serialized on ``_lock``: callers are per-utterance backend threads and the
    classification is tens of milliseconds, so contention is negligible.
    """
    try:
        import torch

        classifier = _get_classifier()
        codes = _codes()
        with _lock:
            wav = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))
            out_prob, _, _, _ = classifier.classify_batch(wav.unsqueeze(0))
        return pick_language(out_prob[0], codes, allowed)
    except Exception as e:  # noqa: BLE001 — detection is best-effort
        log.warning("Language detection failed: %s", e)
        return None
