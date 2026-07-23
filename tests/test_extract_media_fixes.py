"""Regression tests for three attachment-extraction audit fixes.

(1) Media transcription is submitted to the Whisper backend's single-worker
    executor, so the mlx-whisper model is loaded once and shared and media
    decodes are serialized with live ASR (no second unlocked model load, no
    concurrent MLX work with a live recording).
(2) ``WHISPER_HALLUCINATIONS`` is lowercase-normalized: the match site
    lowercases the decoded text, so the previously uppercase entries (e.g.
    "Obrigado", "Oh no") were dead filters until normalized.

The heavy models never load here: mlx-whisper's ``transcribe``/``load_audio``
is mocked, mirroring the harness in ``tests/test_media_extraction.py`` and
``tests/test_asr_backends.py``.
"""

import numpy as np
import pytest

import server.extract.media as media
from server.asr import whisper_backend

# ── Fix 2: WHISPER_HALLUCINATIONS lowercase normalization ─────────────────────


def test_hallucination_set_is_lowercase_normalized():
    # Every entry must equal its own lowercase; the match site lowercases the
    # decoded text before the membership test, so any non-lowercase entry could
    # never match.
    non_lower = [p for p in whisper_backend.WHISPER_HALLUCINATIONS if p != p.lower()]
    assert non_lower == []


def test_uppercase_origin_phrases_now_match_the_filter():
    # These were stored uppercase (dead filters) before normalization. The
    # filter tests ``text.strip().lower() in WHISPER_HALLUCINATIONS``, so the
    # lowered form must be present.
    hset = whisper_backend.WHISPER_HALLUCINATIONS
    for phrase in ("Obrigado", "Gracias", "Oh no", "Oh no!", "Obrigado por assistir!"):
        assert phrase.strip().lower() in hset, phrase


def test_decode_utterance_drops_uppercase_origin_hallucination(monkeypatch):
    # End-to-end through the real match site: a decode that returns the
    # uppercase-origin phrase "Obrigado" is filtered down to empty text.
    monkeypatch.setattr(whisper_backend, "_transcribe", lambda audio, language=None: "Obrigado")
    # PCM16 loud enough to clear ENERGY_THRESHOLD (RMS / 32768 > 0.01).
    pcm = np.full(16000, 1000, dtype=np.int16).tobytes()
    text, audio = whisper_backend._decode_utterance(pcm)
    assert text == ""
    assert isinstance(audio, np.ndarray)


# ── Fix 1: media transcription routed through the shared ASR executor ─────────


class _ImmediateFuture:
    """Stands in for the Future returned by ``executor.submit`` — resolves the
    submitted callable synchronously so the test needs no real thread."""

    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value


def test_media_transcription_goes_through_whisper_executor(monkeypatch):
    mlx_whisper = pytest.importorskip("mlx_whisper")  # CI strips the ML stack

    # No real audio load or model inference: the ffmpeg loader and the decode
    # are both stubbed. Empty segments -> the text path (no diarization).
    monkeypatch.setattr(
        mlx_whisper.audio, "load_audio", lambda path: np.zeros(16000, dtype=np.float32)
    )
    monkeypatch.setattr(
        mlx_whisper,
        "transcribe",
        lambda audio, **kw: {"segments": [], "text": "hello from media"},
    )
    monkeypatch.setattr(whisper_backend, "_ensure_model", lambda: "/dummy/model")
    monkeypatch.setattr("server.infrastructure.config.get", lambda key, default=None: None)

    submitted: list = []

    def _spy_submit(fn, *args, **kwargs):
        submitted.append(fn)
        return _ImmediateFuture(fn(*args, **kwargs))

    # Shadow the bound method on the shared single-worker executor instance.
    monkeypatch.setattr(whisper_backend.executor, "submit", _spy_submit)

    out = media._transcribe_with_speakers("/nonexistent.wav")

    # The decode was routed through the backend's shared executor exactly once
    # (shared model load + serialized with live ASR), not run inline.
    assert len(submitted) == 1
    assert out == "hello from media"
