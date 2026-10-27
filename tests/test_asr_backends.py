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

    def take_discard(self) -> dict | None:
        return None


def test_whisper_session_emits_final_events(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_decode_utterance",
        lambda pcm: ("hello world", np.zeros(16000, dtype=np.float32), "en", []),
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
        lambda pcm: ("", np.zeros(16000, dtype=np.float32), None, []),
    )
    session = whisper_backend.create_session()
    session._buf = _StubBuffer([b"\x00" * 32000], tail=b"\x00" * 32000)
    assert session.process(b"\x00" * 960) == []
    assert session.finish() == []


def test_whisper_finish_flushes_tail(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_decode_utterance",
        lambda pcm: ("the tail", np.zeros(16000, dtype=np.float32), "en", []),
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


def test_repetition_filter_spares_emphasis_inside_a_sentence():
    """A back-to-back triple is a loop only when it is most of the utterance.
    Tripled emphasis inside a real sentence is ordinary speech (Polish above
    all) and must survive; the same phrase repeated as the whole text is
    still a loop, even when it could be real ("Tak, tak, tak." cannot be told
    apart from "Ola ola ola" by its text)."""
    r = whisper_backend.is_repetition_hallucination
    for spoken in (
        "To było bardzo, bardzo, bardzo dobre spotkanie i wszyscy byli zadowoleni z wyników.",
        "Tak, tak, tak, rozumiem, ale budżet na przyszły kwartał jest już zamknięty.",
        "Musimy to zrobić szybko, szybko, szybko, bo klient czeka na odpowiedź od wczoraj.",
        "Nie nie, nie o to mi chodziło.",
        "Nie, nie, nie, ja tego nie powiedziałem.",
        "It was very, very, very good and everyone in the meeting agreed with the plan.",
    ):
        assert not r(spoken), spoken
        # The repeated phrase alone, looping, is still caught.
        clean = spoken.lower().replace(",", "").split()
        run = next(w for i, w in enumerate(clean) if clean[i + 1 : i + 3] == [w, w])
        assert r(" ".join([run] * 4)), run
    # A long loop with a short lead-in stays a loop.
    assert r("and then the the the the the the the the the the")


def test_repetition_filter_counts_whole_words_not_substrings():
    """The long-form rule counts a phrase as whole words. A short word that
    merely recurs inside longer ones ("the" in "there", "ta" in "tabela",
    "nie" in "niebo") is not repetition, while a phrase that really loops is."""
    r = whisper_backend.is_repetition_hallucination
    for spoken in (
        "So then they thought the theme was there.",
        "Ta tabela tam to taka tania tapeta.",
        "Na nas na nasz narodowy naród.",
        "Nie wiem czy niebo nie jest niebieskie nie",
        "Nie, nie mogę, niestety nie dzisiaj.",
        "the cat and the dog and the bird",
    ):
        assert not r(spoken), spoken
    assert r("and then i said i love you i love you i love you i love you")
    assert r("I love you. " * 12)


def test_repetition_filter_needs_back_to_back_repeats():
    """A word or phrase that recurs through a sentence with other words
    between its occurrences is how people talk about one thing, not a loop,
    however often it recurs. The same phrase said back to back until it takes
    over the text is a loop."""
    r = whisper_backend.is_repetition_hallucination
    for spoken in (
        "Nie wiem, naprawdę nie wiem, nie wiem co powiedzieć.",
        "Dziękuję bardzo, dziękuję wszystkim, dziękuję i do widzenia.",
        "Transkrypcja działa, transkrypcja jest szybka, transkrypcja jest dobra.",
        "Nie, nie, nie, to nie tak, nie.",
        "To było bardzo, bardzo, bardzo dobre.",
        "We need the database migration, the database backup and the database restore.",
        "Kubernetes deployment, Kubernetes service and Kubernetes ingress.",
    ):
        assert not r(spoken), spoken
    assert r("Thank you for watching. " * 3)
    assert r("We need the database " + "the database " * 5)


# ── startup warmup policy: ONLY Parakeet is warmed at startup ──────────────────


def _warm_calls(monkeypatch, backend_name, installed=lambda entry: True):
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

    monkeypatch.setattr(_catalog, "is_installed", installed)
    conf = {"transcription_backend": backend_name, "local_mode": False}
    monkeypatch.setattr(cfg, "get", lambda k, default=None: conf.get(k, default))
    main._warm_transcription_models()
    return calls


def test_startup_warms_parakeet_and_the_speaker_encoder(monkeypatch):
    """Default (streaming -> parakeet): Parakeet and the speaker encoder are
    warmed, so the first recorded sentence waits on neither; Whisper is never
    loaded or downloaded."""
    assert _warm_calls(monkeypatch, "streaming") == ["parakeet", "diarization"]


def test_startup_never_downloads_a_missing_speaker_encoder(monkeypatch):
    calls = _warm_calls(monkeypatch, "streaming", installed=lambda e: e.key == "parakeet")
    assert calls == ["parakeet"]


def test_startup_warms_nothing_when_engine_is_whisper(monkeypatch):
    """If the record engine is Whisper, startup loads NOTHING (Whisper stays lazy);
    the Parakeet-only rule never eager-loads Whisper at startup."""
    assert _warm_calls(monkeypatch, "whisper") == []


# ── relaxed rescue pass ─────────────────────────────────


def _speech_pcm() -> bytes:
    """1 s of loud sine — comfortably above the RMS energy gate."""
    t = np.arange(16000, dtype=np.float32) / 16000.0
    return (np.sin(2 * np.pi * 220 * t) * 0.3 * 32767).astype(np.int16).tobytes()


def test_decode_retries_relaxed_when_strict_pass_is_empty(monkeypatch):
    calls = []

    def fake_transcribe(audio, language=None, relaxed=False):
        calls.append((language, relaxed))
        return ("prawdziwy tekst" if relaxed else "", language, [])

    monkeypatch.setattr(whisper_backend, "_transcribe", fake_transcribe)
    text, _, _, _ = whisper_backend._decode_utterance(_speech_pcm())
    assert text == "prawdziwy tekst"
    assert calls == [(None, False), (None, True)]


def test_decode_rescue_is_still_hallucination_filtered(monkeypatch):
    monkeypatch.setattr(
        whisper_backend,
        "_transcribe",
        lambda audio, language=None, relaxed=False: (
            "thank you." if relaxed else "",
            language,
            [],
        ),
    )
    text, _, _, _ = whisper_backend._decode_utterance(_speech_pcm())
    assert text == ""


# ── translate-to-English companion pass ──────────────────────────────────────


def test_translate_utterance_keeps_short_phrases_and_drops_loops(monkeypatch):
    """The source already passed its engine's transcript filter, so a short
    translation that happens to be an English filler phrase ("Dziekuje." ->
    "Thank you.") is real; only a runaway loop is dropped."""
    from server.asr import canary_translator

    audio = np.zeros(16000, dtype=np.float32)
    monkeypatch.setattr(
        canary_translator, "_generate", lambda a, source_lang, target_lang: "Thank you."
    )
    assert canary_translator.translate_utterance(audio, "pl") == "Thank you."
    loop = "thank you thank you thank you thank you thank you thank you"
    monkeypatch.setattr(canary_translator, "_generate", lambda a, source_lang, target_lang: loop)
    assert canary_translator.translate_utterance(audio, "pl") == ""


# ── canary translator ─────────────────────────────────────────


def test_canary_translate_language_pairs(monkeypatch):
    from server.asr import canary_translator

    calls = []

    def fake_generate(audio, source_lang, target_lang):
        calls.append((source_lang, target_lang))
        return "good morning"

    monkeypatch.setattr(canary_translator, "_generate", fake_generate)
    audio = np.zeros(16000, dtype=np.float32)
    assert canary_translator.translate_utterance(audio, "pl") == "good morning"
    assert canary_translator.translate_utterance(audio, "en", target="pl") == "good morning"
    # Same language: skipped (clears the pending slot without a bogus line).
    assert canary_translator.translate_utterance(audio, "en", target="en") == ""
    # X -> Y with both non-English is unsupported: skipped, no decode.
    assert canary_translator.translate_utterance(audio, "pl", target="de") == ""
    assert calls == [("pl", "en"), ("en", "pl")]


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
    # A known source Canary cannot read is not attempted in either direction
    # (only an unknown one is, and detected there).
    for heard in ("ja", "ar", "zh", "no"):
        assert r("canary", False, heard, "en") is None, heard
        assert r("canary", True, heard, "en") is None, heard
    # apple: needs the bridge, any pair
    assert r("apple", True, "pl", "de") == "apple"
    assert r("apple", False, "pl", "en") is None
    assert r("apple", True, None, "pl") == "apple"
    # legacy stored modes map to canary
    assert r("auto", False, "pl", "en") == "canary"
    assert r("model", False, "en", "pl") == "canary"


def test_lid_pick_language_constrained():
    import math

    import pytest

    from server.asr.lid import pick_language

    codes = ("en", "pl", "de", "ru")
    probs = [math.log(p) for p in (0.1, 0.2, 0.6, 0.9)]
    code, conf = pick_language(probs, codes, {"en", "pl"})
    # Confidence is RELATIVE to the candidate set: 0.2 / (0.1 + 0.2), however
    # much of the mass sits on languages outside it.
    assert code == "pl" and abs(conf - 2 / 3) < 0.01
    # Tiny absolute masses keep their ratio (no underflow to a zero share).
    tiny = [-800.0, -799.0, -1.0, -0.5]
    code, conf = pick_language(tiny, codes, {"en", "pl"})
    assert code == "pl" and abs(conf - 1 / (1 + math.exp(-1))) < 1e-6
    # No candidate the classifier can label is a programming error: it
    # raises instead of quietly widening to every language.
    with pytest.raises(ValueError):
        pick_language(probs, codes, {"xx"})


def test_lid_detect_reports_classifier_failure_but_raises_on_no_candidates(monkeypatch, caplog):
    import pytest

    from server.asr import lid

    def broken():
        raise RuntimeError("model missing")

    monkeypatch.setattr(lid, "_get_classifier", broken)
    audio = np.zeros(16000, dtype=np.float32)
    with caplog.at_level("WARNING", logger="whisper-studio"):
        assert lid.detect(audio, ("en", "pl")) == (None, 0.0)
    assert "Language detection failed" in caplog.text
    with pytest.raises(ValueError):
        lid.detect(audio, ())


# ── word timings (what lets the orchestrator split a turn) ───────────────────


def test_whisper_words_are_flattened_from_segments():
    words = whisper_backend._words_from_segments(
        {
            "segments": [
                {"words": [{"word": " hello", "start": 0.0, "end": 0.4}]},
                {
                    "words": [
                        {"word": " there", "start": 0.4, "end": 0.9},
                        {"word": "", "start": 1.0, "end": 1.2},
                        {"word": " late", "start": None, "end": 1.5},
                    ]
                },
            ]
        }
    )
    assert [w["text"] for w in words] == ["hello", "there"]
    assert words[0]["start"] == 0.0 and words[1]["end"] == 0.9


def test_whisper_words_tolerate_a_decode_without_them():
    assert whisper_backend._words_from_segments({}) == []
    assert whisper_backend._words_from_segments({"segments": [{"text": "hi"}]}) == []


def test_parakeet_tokens_merge_into_words():
    from types import SimpleNamespace

    from server.asr import parakeet_backend

    def token(text, start, end):
        return SimpleNamespace(text=text, start=start, end=end)

    result = SimpleNamespace(
        tokens=[
            token(" quar", 0.0, 0.2),
            token("ter", 0.2, 0.35),
            token("ly", 0.35, 0.5),
            token(" numbers", 0.5, 0.9),
        ]
    )
    words = parakeet_backend._words_from_result(result)
    assert [w["text"] for w in words] == ["quarterly", "numbers"]
    assert words[0]["start"] == 0.0 and words[0]["end"] == 0.5


def test_parakeet_words_tolerate_a_result_without_tokens():
    from types import SimpleNamespace

    from server.asr import parakeet_backend

    assert parakeet_backend._words_from_result(SimpleNamespace()) == []


def test_whisper_words_record_whether_a_space_preceded_them():
    words = whisper_backend._words_from_segments(
        {
            "segments": [
                {
                    "words": [
                        {"word": "你好", "start": 0.0, "end": 0.3},
                        {"word": " hello", "start": 0.3, "end": 0.7},
                    ]
                }
            ]
        }
    )
    assert [w["space"] for w in words] == [False, True]
