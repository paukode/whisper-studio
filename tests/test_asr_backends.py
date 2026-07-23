"""Backend registry contract and the Whisper session's event shape.

No model loads: backend modules import lazily and the decode function is
monkeypatched where a session is exercised.
"""

import numpy as np

from server import asr
from server.asr import whisper_backend


def test_resolve_name_aliases_and_fallback():
    assert asr.resolve_name("whisper") == "whisper"
    assert asr.resolve_name("parakeet") == "parakeet"
    assert asr.resolve_name("streaming") == "parakeet"
    assert asr.resolve_name("STREAMING") == "parakeet"
    assert asr.resolve_name(None) == "whisper"
    assert asr.resolve_name("bogus") == "whisper"


def test_backends_expose_contract():
    for name in asr.BACKENDS:
        mod = asr.get_backend(name)
        assert hasattr(mod, "executor"), name
        assert callable(mod.create_session), name
        assert callable(mod.preload), name


class _StubBuffer:
    """Stands in for UtteranceBuffer so no VAD/speech audio is needed."""

    def __init__(self, utterances: list[bytes], tail: bytes | None = None):
        self._utterances = utterances
        self._tail = tail

    def feed(self, chunk: bytes) -> list[bytes]:
        out, self._utterances = self._utterances, []
        return out

    def flush(self) -> bytes | None:
        tail, self._tail = self._tail, None
        return tail


def test_whisper_session_emits_final_events(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_decode_utterance",
        lambda pcm: ("hello world", np.zeros(16000, dtype=np.float32)),
    )
    session = whisper_backend.create_session()
    session._buf = _StubBuffer([b"\x00" * 32000])
    events = session.process(b"\x00" * 960)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "final"
    assert ev["text"] == "hello world"
    assert isinstance(ev["audio"], np.ndarray)


def test_whisper_session_drops_empty_decodes(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_decode_utterance",
        lambda pcm: ("", np.zeros(16000, dtype=np.float32)),
    )
    session = whisper_backend.create_session()
    session._buf = _StubBuffer([b"\x00" * 32000], tail=b"\x00" * 32000)
    assert session.process(b"\x00" * 960) == []
    assert session.finish() == []


def test_whisper_finish_flushes_tail(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_decode_utterance",
        lambda pcm: ("the tail", np.zeros(16000, dtype=np.float32)),
    )
    session = whisper_backend.create_session()
    session._buf = _StubBuffer([], tail=b"\x00" * 32000)
    events = session.finish()
    assert [e["text"] for e in events] == ["the tail"]


def test_repetition_hallucination_filter():
    assert whisper_backend.is_repetition_hallucination("cheers cheers cheers")
    assert whisper_backend.is_repetition_hallucination("i do i do i do")
    assert not whisper_backend.is_repetition_hallucination(
        "The quarterly numbers look better than expected this time."
    )


# ── startup warmup policy: ONLY Parakeet is warmed at startup ──────────────────


def _warm_calls(monkeypatch, backend_name):
    """Run _warm_transcription_models with all preloads stubbed; return the list of
    models that were (would have been) loaded, in order."""
    import server.asr.parakeet_backend as pk
    import server.asr.whisper_backend as wh
    from server import main
    from server.infrastructure import config as cfg

    calls: list[str] = []
    monkeypatch.setattr(pk, "preload", lambda: calls.append("parakeet"))
    monkeypatch.setattr(wh, "preload", lambda: calls.append("whisper"))
    monkeypatch.setattr(wh, "_ensure_model", lambda: calls.append("whisper-download"))
    try:
        import server.diarization as diar

        monkeypatch.setattr(diar, "preload", lambda: calls.append("diarization"))
    except Exception:
        pass
    conf = {"transcription_backend": backend_name, "local_mode": False}
    monkeypatch.setattr(cfg, "get", lambda k, default=None: conf.get(k, default))
    main._warm_transcription_models()
    return calls


def test_startup_warms_only_parakeet(monkeypatch):
    """Default (streaming -> parakeet): only Parakeet is warmed; Whisper is never
    loaded or downloaded, and the speaker/diarization encoder is not preloaded."""
    assert _warm_calls(monkeypatch, "streaming") == ["parakeet"]


def test_startup_warms_nothing_when_engine_is_whisper(monkeypatch):
    """If the record engine is Whisper, startup loads NOTHING (Whisper stays lazy);
    the Parakeet-only rule never eager-loads Whisper at startup."""
    assert _warm_calls(monkeypatch, "whisper") == []
