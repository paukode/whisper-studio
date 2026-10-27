"""Canary as the translator only.

Canary never transcribes: Whisper or Parakeet write the transcript and
Canary turns the same audio into the translation line. A Whisper line
carries its language; a Parakeet line carries none, and language ID picks
its source among every Canary language, English first.

No model loads: ``lid.detect`` and ``_generate`` are scripted where
canary_translator resolves them.
"""

import numpy as np
import pytest

from server.asr import canary_translator, lid
from server.asr.canary_translator import CANARY_LANGUAGES

SR = 16000
AUDIO = np.zeros(SR, dtype=np.float32)


def _scripted_lid(monkeypatch, answer):
    """lid.detect answers ``answer`` and records every candidate set."""
    offered = []

    def fake_detect(audio, candidates):
        offered.append(tuple(candidates))
        return answer

    monkeypatch.setattr(lid, "detect", fake_detect)
    return offered


@pytest.fixture
def calls(monkeypatch):
    """(source, target) of every Canary decode; each one says Good morning."""
    seen = []

    def fake_generate(audio, source_lang, target_lang):
        seen.append((source_lang, target_lang))
        return "Good morning."

    monkeypatch.setattr(canary_translator, "_generate", fake_generate)
    return seen


def test_a_parakeet_line_is_detected_among_every_canary_language(monkeypatch, calls):
    offered = _scripted_lid(monkeypatch, ("pl", 0.9))
    assert canary_translator.translate_utterance(AUDIO, None, target="en") == "Good morning."
    assert calls == [("pl", "en")]
    assert set(offered[0]) == CANARY_LANGUAGES and offered[0][0] == "en"


def test_a_whisper_line_keeps_its_own_language(monkeypatch, calls):
    offered = _scripted_lid(monkeypatch, ("de", 0.9))
    assert canary_translator.translate_utterance(AUDIO, "pl", target="en") == "Good morning."
    assert offered == [] and calls == [("pl", "en")]


@pytest.mark.parametrize("answer", [("en", 0.9), (None, 0.0)])
def test_a_source_equal_to_the_target_is_skipped(monkeypatch, calls, answer):
    """A source detected as the target, or one the classifier could not name
    (English heads the search), gives no line."""
    _scripted_lid(monkeypatch, answer)
    assert canary_translator.translate_utterance(AUDIO, None, target="en") == ""
    assert calls == []


def test_a_pair_without_english_is_skipped(monkeypatch, calls):
    assert canary_translator.translate_utterance(AUDIO, "pl", target="de") == ""
    assert calls == []


def test_a_failed_language_id_load_is_retried_once_per_interval(monkeypatch, caplog):
    now = [1000.0]
    attempts = []

    def missing():
        attempts.append(now[0])
        raise OSError("model missing and offline")

    monkeypatch.setattr(lid, "_clock", lambda: now[0])
    monkeypatch.setattr(lid, "_classifier", None)
    monkeypatch.setattr(lid, "_load_failure", None)
    monkeypatch.setattr(lid, "_ensure_model", missing)
    with caplog.at_level("WARNING", logger="whisper-studio"):
        for _ in range(5):  # several translated lines in a row
            assert lid.detect(AUDIO, ("en", "pl")) == (None, 0.0)
        assert len(attempts) == 1
        now[0] += lid._LOAD_RETRY_SECONDS
        assert lid.detect(AUDIO, ("en", "pl")) == (None, 0.0)
    assert len(attempts) == 2
    assert caplog.text.count("Language detection failed") == 2
