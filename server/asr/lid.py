"""Spoken-language identification: VoxLingua107 ECAPA via speechbrain.

Canary has no language head (its decoder needs an explicit source-language
token), so this small classifier supplies it: ~80 MB, 107 languages, and
~40-60 ms per utterance on CPU (probed on-device). CPU on purpose: MPS is
contended by the ASR models and the index embedder, and at this size CPU is
faster than paying the transfer.

``detect`` always picks among the caller's candidates (the transcription
languages the engine decodes, see server/asr/languages.py), the same trick
the Whisper backend uses for its own detection: the model still scores all
107 languages, we just never pick one the session can't use. There is no
"all languages" mode: an empty candidate set, or one the classifier has no
label for, is a programming error and raises.
"""

from __future__ import annotations

import logging
import os
import threading
import time
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

# A failed load (model missing and not downloadable, a speechbrain import
# error) is remembered for this long. Its callers sit on Canary's single
# decode thread, once per final and per long draft, so without this every
# one of them would retry the load, download attempt included, and log it.
_LOAD_RETRY_SECONDS = 60.0
_clock = time.monotonic
_load_failure: tuple[float, str] | None = None  # (when, why) of the last failed load


class ClassifierUnavailable(RuntimeError):
    """The classifier failed to load recently; the load is not retried yet."""


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
    global _classifier, _load_failure
    if _classifier is not None:
        return _classifier
    with _lock:
        if _classifier is None:
            if _load_failure is not None and _clock() - _load_failure[0] < _LOAD_RETRY_SECONDS:
                raise ClassifierUnavailable(_load_failure[1])
            try:
                _ensure_model()
                log.info("Loading language-ID model...")
                from speechbrain.inference.classifiers import EncoderClassifier

                _classifier = EncoderClassifier.from_hparams(
                    source=os.path.abspath(LID_MODEL_DIR),
                    savedir=os.path.abspath(LID_MODEL_DIR),
                    run_opts={"device": "cpu"},
                )
            except Exception as e:
                _load_failure = (_clock(), f"{type(e).__name__}: {e}")
                raise
            _load_failure = None
            log.info("Language-ID model loaded.")
    return _classifier


@lru_cache(maxsize=1)
def _codes() -> tuple[str, ...]:
    """ISO codes in classifier-output order (labels look like 'pl: Polish')."""
    ind2lab = _get_classifier().hparams.label_encoder.ind2lab
    return tuple(ind2lab[i].split(":")[0].strip() for i in range(len(ind2lab)))


def pick_language(probs, codes: tuple[str, ...], candidates: Collection[str]) -> tuple[str, float]:
    """(code, relative confidence): argmax over ``codes`` restricted to
    ``candidates``, pure and unit-testable.

    The confidence is the winner's share of the probability mass WITHIN the
    candidate set (classifier probs are log-space over all 107 classes; a
    correct pick from a small candidate set can carry a tiny absolute
    probability, since accented speech puts most of the mass on languages
    outside the set, so absolute thresholds would misjudge constrained
    picks). Raises ValueError when no candidate is a classifier label.
    """
    indices = [i for i, c in enumerate(codes) if c in candidates]
    if not indices:
        raise ValueError(f"no language-ID label for any candidate in {sorted(candidates)}")
    logs = [float(probs[i]) for i in indices]
    top = max(logs)
    # Shift by the max before exponentiating so tiny in-set masses keep
    # their ratios instead of underflowing to zero.
    weights = [float(np.exp(v - top)) for v in logs]
    best = max(range(len(indices)), key=lambda j: weights[j])
    return codes[indices[best]], weights[best] / sum(weights)


def detect(audio: np.ndarray, candidates: Collection[str]) -> tuple[str | None, float]:
    """(language code, relative confidence) for one float32 mono 16 kHz
    utterance, picked among ``candidates``; (None, 0.0), with a warning,
    when the classifier itself fails. After a failed load, calls within
    ``_LOAD_RETRY_SECONDS`` return (None, 0.0) at once, without retrying it.

    Serialized on ``_lock``: callers are per-utterance backend threads and the
    classification is tens of milliseconds, so contention is negligible.
    """
    if not candidates:
        raise ValueError("language ID needs at least one candidate language")
    try:
        # Load first, so a missing torch or speechbrain also counts as a
        # failed load and waits out the retry interval.
        classifier = _get_classifier()
        codes = _codes()
        import torch

        with _lock:
            wav = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))
            out_prob, _, _, _ = classifier.classify_batch(wav.unsqueeze(0))
    except ClassifierUnavailable as e:
        log.debug("Language detection unavailable until the load is retried: %s", e)
        return None, 0.0
    except Exception as e:  # noqa: BLE001 (a classifier failure is reported, not raised)
        log.warning("Language detection failed: %s", e)
        return None, 0.0
    # Outside the try: a candidate set the classifier cannot label is a bug
    # in the caller and must surface, not read as a detection failure.
    return pick_language(out_prob[0], codes, candidates)
