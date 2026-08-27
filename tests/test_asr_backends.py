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
        lambda pcm: ("hello world", np.zeros(16000, dtype=np.float32), "en"),
    )
    session = whisper_backend.create_session()
    session._buf = _StubBuffer([b"\x00" * 32000])
    events = session.process(b"\x00" * 960)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "final"
    assert ev["text"] == "hello world"
    assert ev["language"] == "en"
    assert isinstance(ev["audio"], np.ndarray)


def test_whisper_session_drops_empty_decodes(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_decode_utterance",
        lambda pcm: ("", np.zeros(16000, dtype=np.float32), None),
    )
    session = whisper_backend.create_session()
    session._buf = _StubBuffer([b"\x00" * 32000], tail=b"\x00" * 32000)
    assert session.process(b"\x00" * 960) == []
    assert session.finish() == []


def test_whisper_finish_flushes_tail(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_decode_utterance",
        lambda pcm: ("the tail", np.zeros(16000, dtype=np.float32), "en"),
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
    # Warmup only fires when the weights are already downloaded; force that so
    # the test exercises the engine-routing logic regardless of whether the
    # 2.3 GB Parakeet weights physically exist under app_home (they never do
    # under a throwaway WHISPER_HOME / on CI).
    from server.models_manager import catalog as _catalog

    monkeypatch.setattr(_catalog, "is_installed", lambda entry: True)
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


# ── language allowlist + relaxed rescue pass ─────────────────────────────────


def test_parse_languages():
    assert whisper_backend._parse_languages(None) == []
    assert whisper_backend._parse_languages("") == []
    assert whisper_backend._parse_languages("pl") == ["pl"]
    assert whisper_backend._parse_languages(" PL , en ,") == ["pl", "en"]


def test_configured_languages_drops_unknown_codes(monkeypatch):
    monkeypatch.setattr(whisper_backend, "config_get", lambda k: "pl,nope,en")
    assert whisper_backend._configured_languages() == ["pl", "en"]


def test_pick_language_ignores_disallowed():
    probs = {"ru": 0.5, "pl": 0.3, "en": 0.2}
    assert whisper_backend._pick_language(probs, ["pl", "en"]) == "pl"


def _speech_pcm() -> bytes:
    """1 s of loud sine — comfortably above the RMS energy gate."""
    t = np.arange(16000, dtype=np.float32) / 16000.0
    return (np.sin(2 * np.pi * 220 * t) * 0.3 * 32767).astype(np.int16).tobytes()


def test_decode_retries_relaxed_when_strict_pass_is_empty(monkeypatch):
    calls = []

    def fake_transcribe(audio, language=None, relaxed=False):
        calls.append((language, relaxed))
        return ("prawdziwy tekst" if relaxed else "", language)

    monkeypatch.setattr(whisper_backend, "_transcribe", fake_transcribe)
    monkeypatch.setattr(whisper_backend, "_configured_languages", lambda: [])
    text, _, _ = whisper_backend._decode_utterance(_speech_pcm())
    assert text == "prawdziwy tekst"
    assert calls == [(None, False), (None, True)]


def test_decode_rescue_is_still_hallucination_filtered(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_transcribe",
        lambda audio, language=None, relaxed=False: (
            "thank you." if relaxed else "",
            language,
        ),
    )
    monkeypatch.setattr(whisper_backend, "_configured_languages", lambda: [])
    text, _, _ = whisper_backend._decode_utterance(_speech_pcm())
    assert text == ""


def test_decode_uses_constrained_detection_for_allowlist(monkeypatch):
    seen = {}
    monkeypatch.setattr(whisper_backend, "_configured_languages", lambda: ["pl", "en"])
    monkeypatch.setattr(whisper_backend, "_detect_language", lambda audio, allowed: "pl")

    def fake_transcribe(audio, language=None, relaxed=False):
        seen["language"] = language
        return "dzień dobry wszystkim", language

    monkeypatch.setattr(whisper_backend, "_transcribe", fake_transcribe)
    text, _, _ = whisper_backend._decode_utterance(_speech_pcm())
    assert text == "dzień dobry wszystkim"
    assert seen["language"] == "pl"


# ── translate-to-English companion pass ──────────────────────────────────────


def test_translate_utterance_filters_hallucinations(monkeypatch):
    from server.asr import canary_backend

    monkeypatch.setattr(
        canary_backend, "_generate", lambda a, source_lang, target_lang: "thank you."
    )
    assert canary_backend.translate_utterance(np.zeros(16000, dtype=np.float32), "pl") == ""


# ── canary backend + whisper variant ─────────────────────────────────────────


def test_resolve_name_canary():
    assert asr.resolve_name("canary") == "canary"


def test_canary_session_emits_final_with_language(monkeypatch):
    from server.asr import canary_backend

    monkeypatch.setattr(
        canary_backend,
        "_decode_utterance",
        lambda pcm: ("dzień dobry", np.zeros(16000, dtype=np.float32), "pl"),
    )
    session = canary_backend.create_session()
    session._buf = _StubBuffer([b"\x00" * 32000])
    events = session.process(b"\x00" * 960)
    assert [(e["kind"], e["text"], e["language"]) for e in events] == [
        ("final", "dzień dobry", "pl")
    ]


def test_canary_utterance_language_pins_and_detects(monkeypatch):
    import server.infrastructure.config as cfg
    from server.asr import canary_backend, lid

    audio = np.zeros(16000, dtype=np.float32)
    # Single supported allowlist entry: pinned, no detection call.
    monkeypatch.setattr(cfg, "get", lambda k, default=None: "xx,pl")
    monkeypatch.setattr(
        lid, "detect", lambda a, allowed=None: (_ for _ in ()).throw(AssertionError)
    )
    assert canary_backend._utterance_language(audio) == "pl"
    # Multi-entry allowlist: detection constrained to it.
    seen = {}

    def fake_detect(a, allowed=None):
        seen["allowed"] = set(allowed)
        return "en"

    monkeypatch.setattr(cfg, "get", lambda k, default=None: "pl,en")
    monkeypatch.setattr(lid, "detect", fake_detect)
    assert canary_backend._utterance_language(audio) == "en"
    assert seen["allowed"] == {"pl", "en"}
    # No allowlist: detection over all Canary languages; failure falls to en.
    monkeypatch.setattr(cfg, "get", lambda k, default=None: "")
    monkeypatch.setattr(lid, "detect", lambda a, allowed=None: None)
    assert canary_backend._utterance_language(audio) == "en"


def test_canary_translate_language_pairs(monkeypatch):
    from server.asr import canary_backend

    calls = []

    def fake_generate(audio, source_lang, target_lang):
        calls.append((source_lang, target_lang))
        return "good morning"

    monkeypatch.setattr(canary_backend, "_generate", fake_generate)
    audio = np.zeros(16000, dtype=np.float32)
    assert canary_backend.translate_utterance(audio, "pl") == "good morning"
    assert canary_backend.translate_utterance(audio, "en", target="pl") == "good morning"
    # Same language: skipped (clears the pending slot without a bogus line).
    assert canary_backend.translate_utterance(audio, "en", target="en") == ""
    # X -> Y with both non-English is unsupported: skipped, no decode.
    assert canary_backend.translate_utterance(audio, "pl", target="de") == ""
    assert calls == [("pl", "en"), ("en", "pl")]


def test_canary_translate_detects_unknown_source(monkeypatch):
    from server.asr import canary_backend, lid

    monkeypatch.setattr(canary_backend, "_generate", lambda a, source_lang, target_lang: "hi")
    monkeypatch.setattr(lid, "detect", lambda a, allowed=None: "pl")
    import server.infrastructure.config as cfg

    monkeypatch.setattr(cfg, "get", lambda k, default=None: "")
    audio = np.zeros(16000, dtype=np.float32)
    # Parakeet finals carry no language: detected here, then translated.
    assert canary_backend.translate_utterance(audio, None, target="en") == "hi"
    # Detected language equals the target: skipped.
    monkeypatch.setattr(lid, "detect", lambda a, allowed=None: "en")
    assert canary_backend.translate_utterance(audio, None, target="en") == ""


# ── translate-mode resolution (server/websocket.py) ─────────────────────────


def test_resolve_translator_matrix():
    from server.websocket import resolve_translator as r

    # off, and same-language skips
    assert r("off", True, "pl", "en") is None
    assert r("canary", True, "en", "en") is None
    assert r("apple", True, "pl", "pl") is None
    # canary: English-hub bidirectional, unknown source attempted
    assert r("canary", False, "pl", "en") == "canary"
    assert r("canary", False, "en", "pl") == "canary"
    assert r("canary", False, "pl", "de") is None
    assert r("canary", False, None, "de") == "canary"
    assert r("canary", False, None, "en") == "canary"
    assert r("canary", False, "pl", "ja") is None  # ja not a Canary language
    # apple: needs the bridge, any pair
    assert r("apple", True, "pl", "de") == "apple"
    assert r("apple", False, "pl", "en") is None
    assert r("apple", True, None, "pl") == "apple"
    # legacy stored modes map to canary
    assert r("auto", False, "pl", "en") == "canary"
    assert r("model", False, "en", "pl") == "canary"


def test_lid_pick_language_constrained():
    from server.asr.lid import pick_language

    codes = ("en", "pl", "de", "ru")
    probs = [0.1, 0.2, 0.6, 0.9]
    assert pick_language(probs, codes, None) == "ru"
    assert pick_language(probs, codes, {"en", "pl"}) == "pl"
    # Empty intersection falls back to the global argmax.
    assert pick_language(probs, codes, {"xx"}) == "ru"
