"""The cross-session voiceprint gallery.

Every test redirects the store into tmp_path and pins the active encoder,
so nothing here touches the real gallery or loads a model.
"""

import numpy as np
import pytest

from server.diarization import embedder, voiceprints

# Captured at import, before the autouse fixture below pins it to True, so the
# default-off tests exercise the real gate rather than the fixture's stub.
_REAL_ENABLED = voiceprints.enabled


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(voiceprints, "data_root", lambda: str(tmp_path))
    monkeypatch.setattr(voiceprints, "_active_encoder", lambda: embedder.NAME_REDIMNET)
    monkeypatch.setattr(voiceprints, "enabled", lambda: True)


def vec(*values: float) -> np.ndarray:
    v = np.array(values, dtype=np.float32)
    return v / np.linalg.norm(v)


ANNA = vec(1.0, 0.0, 0.0)
BEN = vec(0.0, 1.0, 0.0)


def test_enroll_then_match_returns_the_name():
    assert voiceprints.enroll("Anna", [ANNA, ANNA]) is True
    assert voiceprints.match(ANNA) == "Anna"


def test_unknown_voice_matches_nobody():
    voiceprints.enroll("Anna", [ANNA, ANNA])
    assert voiceprints.match(BEN) is None


def test_a_single_embedding_is_too_thin_to_enroll():
    """One utterance mostly encodes that utterance's acoustics, not the person."""
    assert voiceprints.enroll("Anna", [ANNA]) is False
    assert voiceprints.match(ANNA) is None


def test_ambiguous_match_is_declined():
    """Two stored voices equally close: naming either one would be a coin flip."""
    voiceprints.enroll("Anna", [ANNA, ANNA])
    voiceprints.enroll("Ben", [BEN, BEN])
    assert voiceprints.match(vec(1.0, 1.0, 0.0)) is None


def test_re_enrolling_strengthens_rather_than_replaces():
    voiceprints.enroll("Anna", [ANNA, ANNA])
    voiceprints.enroll("Anna", [vec(0.9, 0.1, 0.0)] * 2)
    assert voiceprints.names() == ["Anna"]
    assert voiceprints.match(ANNA) == "Anna"


def test_other_encoders_gallery_is_invisible(monkeypatch):
    """Vectors from different encoders are not comparable, so they never meet."""
    voiceprints.enroll("Anna", [ANNA, ANNA])
    monkeypatch.setattr(voiceprints, "_active_encoder", lambda: embedder.NAME_ECAPA)
    assert voiceprints.match(ANNA) is None
    assert voiceprints.names() == []


def test_forget_removes_one_identity():
    voiceprints.enroll("Anna", [ANNA, ANNA])
    voiceprints.enroll("Ben", [BEN, BEN])
    assert voiceprints.forget("Anna") is True
    assert voiceprints.names() == ["Ben"]
    assert voiceprints.forget("Anna") is False


def test_disabled_gallery_neither_reads_nor_writes(monkeypatch):
    monkeypatch.setattr(voiceprints, "enabled", lambda: False)
    assert voiceprints.enroll("Anna", [ANNA, ANNA]) is False
    assert voiceprints.match(ANNA) is None


def test_corrupt_store_is_survivable(tmp_path):
    (tmp_path / voiceprints.STORE_NAME).write_text("{ not json")
    assert voiceprints.match(ANNA) is None
    assert voiceprints.enroll("Anna", [ANNA, ANNA]) is True
    assert voiceprints.match(ANNA) == "Anna"


def test_gallery_is_off_by_default():
    """Cross-session recognition is opt-in. Putting a name on the wrong person
    is worse than leaving them as "Speaker 3", so out of the box every
    recording starts from "Speaker 1" again."""
    from server.infrastructure.config import DEFAULTS

    assert DEFAULTS["speaker_voiceprints"] is False


def test_absent_config_key_reads_as_off(monkeypatch):
    """A missing key must not fall back to on: no silent enable."""
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(config_mod, "get", lambda key, default=None: None)
    assert _REAL_ENABLED() is False
