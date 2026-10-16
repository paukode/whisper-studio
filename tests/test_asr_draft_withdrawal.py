"""A live draft whose utterance closes without a final is withdrawn.

Canary and Parakeet show a volatile draft of the in-flight utterance. When
that utterance closes and produces no final (the junk filter dropped it, the
decode was empty, or the VAD found too little voiced audio), nothing would
replace the draft: it stayed on screen and Stop saved it as a segment. The
backend now emits an empty interim for it, and only then.

The VAD case runs through the real UtteranceBuffer: a sub-minimum utterance
is discarded mid-stream without any flush, so only its effect on pending()
shows that the draft's utterance is gone.

Every withdrawal also says why (``withdrawn``), for the recording log: the
words that left the screen and the reason the backend had no final.
"""

import numpy as np

from server.asr import canary_backend, parakeet_backend
from server.audio_buffer import FRAME_BYTES, FRAME_MS, MIN_UTTERANCE_MS, UtteranceBuffer
from server.infrastructure import config as config_mod

_DRAFT = "no no no"
_NO_WORDS = {"draft": _DRAFT, "reason": "no words in the final decode", "decoded": [""]}
_VAD_AT_STOP = {"voiced_ms": 300, "length_ms": 900, "at": "stop"}
_IN_FLIGHT = b"\x01\x00" * 16000  # 1 s in flight: long enough for a draft
_CLOSED = b"\x00\x00" * 32000  # 2 s utterance that just closed


class _Buffer:
    """UtteranceBuffer stand-in: ``closes`` complete on the next feed,
    ``tail`` is what a stop flushes (None when the VAD drops it), and
    ``discard`` is the VAD's record of a drop."""

    def __init__(self) -> None:
        self.closes: list[bytes] = []
        self.tail: bytes | None = None
        self.discard: dict | None = None

    def take_discard(self) -> dict | None:
        discard, self.discard = self.discard, None
        return discard

    def feed(self, chunk: bytes) -> list[bytes]:
        out, self.closes = self.closes, []
        return out

    def pending(self) -> bytes:
        return _IN_FLIGHT

    def flush(self) -> bytes | None:
        tail, self.tail = self.tail, None
        return tail


def _canary(monkeypatch, final_text: str):
    """Canary pinned to Polish (no language ID runs). The in-flight window
    decodes as the draft; a closed utterance as ``final_text``."""
    monkeypatch.setattr(
        config_mod,
        "get",
        lambda key, default=None: "pl" if key == "whisper_language" else default,
    )
    monkeypatch.setattr(canary_backend, "_pending_translations", 0)
    monkeypatch.setattr(
        canary_backend,
        "_generate",
        lambda audio, source_lang, target_lang: _DRAFT if len(audio) == 16000 else final_text,
    )
    session = canary_backend.create_session()
    session._buf = _Buffer()
    return session


def _parakeet(final_text: str):
    session = parakeet_backend.ParakeetSession.__new__(parakeet_backend.ParakeetSession)
    session._buf = _Buffer()
    session._last_interim = ""

    def decode(pcm: bytes):
        text = _DRAFT if pcm == _IN_FLIGHT else final_text
        return text, np.zeros(len(pcm) // 2, dtype=np.float32), []

    session._decode = decode
    return session


def _show_draft(session) -> None:
    assert session.process(b"\x00" * 960) == [{"kind": "interim", "text": _DRAFT}]


def _why_withdrawn(events: list[dict]) -> dict:
    """The events are exactly one withdrawal (an empty interim); its reason."""
    assert [(e["kind"], e["text"]) for e in events] == [("interim", "")]
    return events[0]["withdrawn"]


def test_canary_withdraws_a_draft_whose_utterance_had_no_final(monkeypatch):
    session = _canary(monkeypatch, final_text="")
    _show_draft(session)
    session._buf.closes = [_CLOSED]
    why = _why_withdrawn(session.process(b"\x00" * 960))
    # The lost sentence's own audio rides along, for the opt-in diagnosis.
    assert why.pop("pcm") == _CLOSED
    assert why == _NO_WORDS
    # Nothing is on screen any more, so the next empty close says nothing.
    session._buf.closes = [_CLOSED]
    assert session.process(b"\x00" * 960) == []


def test_canary_final_replaces_its_draft_without_a_withdrawal(monkeypatch):
    session = _canary(monkeypatch, final_text="dzień dobry")
    _show_draft(session)
    session._buf.closes = [_CLOSED]
    events = session.process(b"\x00" * 960)
    assert [(e["kind"], e["text"]) for e in events] == [("final", "dzień dobry")]


def test_canary_stop_withdraws_a_draft_the_tail_cannot_finalize(monkeypatch):
    session = _canary(monkeypatch, final_text="")
    _show_draft(session)
    # Stop with the tail below the VAD minimum: flush() returns None.
    session._buf.discard = dict(_VAD_AT_STOP)
    assert _why_withdrawn(session.finish()) == {
        "draft": _DRAFT,
        "reason": "too little speech for the VAD",
        **_VAD_AT_STOP,
    }
    assert session.finish() == []


def test_parakeet_withdraws_a_draft_whose_utterance_had_no_final():
    session = _parakeet(final_text="")
    _show_draft(session)
    session._buf.closes = [_CLOSED]
    why = _why_withdrawn(session.process(b"\x00" * 960))
    assert why.pop("pcm") == _CLOSED
    assert why == _NO_WORDS


def test_parakeet_stop_keeps_a_real_tail_and_withdraws_a_lost_one():
    session = _parakeet(final_text="tak")
    _show_draft(session)
    session._buf.tail = _CLOSED
    assert [e["kind"] for e in session.finish()] == ["final"]

    session = _parakeet(final_text="")
    _show_draft(session)
    session._buf.discard = dict(_VAD_AT_STOP)
    assert _why_withdrawn(session.finish())["reason"] == "too little speech for the VAD"


# ── the VAD discards an utterance that was already showing a draft ───────


class _ScriptedVad:
    """webrtcvad stand-in: one scripted speech verdict per 30 ms frame."""

    def __init__(self, verdicts: list[bool]) -> None:
        self._verdicts = verdicts

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        return self._verdicts.pop(0)


def _frames(ms: int) -> bytes:
    return b"\x10\x00" * (FRAME_BYTES // 2) * (ms // FRAME_MS)


def _short_utterance(buf: UtteranceBuffer) -> None:
    """Script the buffer's VAD: a short "Tak." (less voiced audio than the
    VAD keeps), then a pause long enough to close it."""
    voiced_ms = MIN_UTTERANCE_MS - 100
    buf._vad = _ScriptedVad([True] * (voiced_ms // FRAME_MS) + [False] * (600 // FRAME_MS))


def test_canary_withdraws_a_draft_the_vad_discards(monkeypatch):
    session = _canary(monkeypatch, final_text="")
    monkeypatch.setattr(canary_backend, "_generate", lambda audio, source_lang, target_lang: "Tak.")
    session._buf = UtteranceBuffer()
    _short_utterance(session._buf)
    # Voicing plus the start of the pause: long enough for a draft.
    assert session.process(_frames(510)) == [{"kind": "interim", "text": "Tak."}]
    # The pause closes the utterance and the VAD discards it: no final.
    why = _why_withdrawn(session.process(_frames(390)))
    assert why["draft"] == "Tak."
    assert why["reason"] == "too little speech for the VAD"
    assert why["voiced_ms"] < MIN_UTTERANCE_MS
    assert why["at"] == "pause"
    assert session._last_interim == ""


def test_parakeet_withdraws_a_draft_the_vad_discards():
    session = _parakeet(final_text="")
    session._decode = lambda pcm: ("Tak", np.zeros(len(pcm) // 2, dtype=np.float32), [])
    session._buf = UtteranceBuffer(trailing_silence_ms=parakeet_backend._TRAILING_SILENCE_MS)
    _short_utterance(session._buf)
    assert session.process(_frames(330)) == [{"kind": "interim", "text": "Tak"}]
    why = _why_withdrawn(session.process(_frames(570)))
    assert why["draft"] == "Tak"
    assert why["reason"] == "too little speech for the VAD"
    assert why["voiced_ms"] < MIN_UTTERANCE_MS
    assert why["at"] == "pause"
    assert session._last_interim == ""


def test_the_vad_reports_each_drop_once_with_where_it_happened():
    buf = UtteranceBuffer()
    buf._vad = _ScriptedVad([True] * 5)
    assert buf.feed(_frames(150)) == []
    assert buf.take_discard() is None  # nothing dropped yet: still in flight
    assert buf.flush() is None  # stop with too little voiced audio
    assert buf.take_discard() == {"voiced_ms": 150, "length_ms": 150, "at": "stop"}
    assert buf.take_discard() is None
