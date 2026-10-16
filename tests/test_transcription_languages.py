"""Transcription languages and Canary's per-take language tracker.

Canary follows its target-language token rather than the audio, so a wrong
language pick is a fluent translation (Polish decoded as ``ru`` comes out as
Russian). The contracts here: a blank setting means English plus the Mac's
languages for a transcript (never open detection over all 25), only strong
evidence settles or switches a take's language, drafts and empty decodes
never move it, and the settled language survives a reconnect but not an
explicit stop.

No model loads: ``lid.detect`` and ``_generate`` are scripted where
canary_backend resolves them, and the Mac language list comes from a
throwaway plist (tests/conftest.py pins an English-only one by default).
"""

import os
import plistlib

import numpy as np
import pytest

from server.asr import canary_backend, languages, lid
from server.asr.canary_backend import CANARY_LANGUAGES

SR = 16000


@pytest.fixture(autouse=True)
def _fresh_language_state(monkeypatch):
    monkeypatch.setattr(languages, "_trackers", {})
    monkeypatch.setattr(languages, "_warned", set())
    monkeypatch.setattr(canary_backend, "_pending_translations", 0)


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """mac(["en-US", "pl-PL"]) points the Mac language list at a plist with
    those AppleLanguages; calling it again rewrites the same file."""
    path = tmp_path / "GlobalPreferences.plist"
    monkeypatch.setattr(languages, "_MAC_GLOBAL_PREFS", str(path))
    stamp = [1_700_000_000]

    def write(tags):
        with open(path, "wb") as f:
            plistlib.dump({"AppleLanguages": list(tags)}, f)
        stamp[0] += 10  # a distinct mtime even within one clock tick
        os.utime(path, (stamp[0], stamp[0]))
        return str(path)

    return write


@pytest.fixture
def setting(monkeypatch):
    """setting("pl,en") sets whisper_language where languages.py reads it."""
    import server.infrastructure.config as cfg

    value = {"whisper_language": ""}
    real_get = cfg.get

    def fake_get(key, default=None):
        if key == "whisper_language":
            return value["whisper_language"]
        return real_get(key, default)

    monkeypatch.setattr(cfg, "get", fake_get)

    def set_value(raw):
        value["whisper_language"] = raw

    return set_value


def _pcm(seconds: float) -> bytes:
    return (np.ones(int(seconds * SR), dtype=np.int16) * 1000).tobytes()


class _Utterances:
    """Stands in for the VAD buffer: each feed closes the queued utterances."""

    def __init__(self, *seconds: float):
        self._queue = [_pcm(s) for s in seconds]
        self.window = b""

    def feed(self, chunk):
        out, self._queue = self._queue, []
        return out

    def pending(self):
        return self.window

    def flush(self):
        return None

    def take_discard(self):
        return None


def _scripted_lid(monkeypatch, script):
    """lid.detect answers ``script(seconds)`` and records every candidate set."""
    offered = []

    def fake_detect(audio, candidates):
        offered.append(tuple(candidates))
        return script(len(audio) / SR)

    monkeypatch.setattr(lid, "detect", fake_detect)
    return offered


class _Calls(list):
    """(source, target) of every decode that produced the output, plus
    ``tie_breaks``: the languages each unsure clip was decoded in."""

    def __init__(self) -> None:
        super().__init__()
        self.tie_breaks: list[tuple[str, ...]] = []


def _recording_generate(monkeypatch, text=lambda lang, seconds: f"<{lang}>", prefer=None):
    """_generate returns text(lang, seconds) and records (src, tgt) pairs.

    The tie-break (_generate_best, for clips language ID is unsure about)
    keeps the language ``prefer(languages)`` names, by default the
    tracker's own pick (the first): the decoder agrees unless a test says
    which language the audio really is."""
    calls = _Calls()

    def fake_generate(audio, source_lang, target_lang):
        calls.append((source_lang, target_lang))
        return text(source_lang, len(audio) / SR)

    def fake_best(audio, languages_to_try):
        calls.tie_breaks.append(tuple(languages_to_try))
        lang = prefer(languages_to_try) if prefer else languages_to_try[0]
        calls.append((lang, lang))
        scores = {code: (-0.1 if code == lang else -2.0) for code in languages_to_try}
        return lang, text(lang, len(audio) / SR), scores

    monkeypatch.setattr(canary_backend, "_generate", fake_generate)
    monkeypatch.setattr(canary_backend, "_generate_best", fake_best)
    return calls


def _finals(session, *seconds):
    session._buf = _Utterances(*seconds)
    return session.process(b"\x00" * 960)


# ── candidate resolution ───────────────────────────────────────────────────


def test_blank_setting_resolves_to_english_plus_mac_languages(mac, setting):
    mac(["en-US", "pl-PL", "ckb-PL"])
    setting("")
    got = languages.resolve(CANARY_LANGUAGES, "Canary")
    mac_codes = {tag.split("-")[0] for tag in ["en-US", "pl-PL", "ckb-PL"]}
    assert got.source == "auto"
    assert got.languages[0] == "en"
    assert set(got.languages) <= CANARY_LANGUAGES
    # Every Mac language Canary decodes is offered, and nothing else is.
    assert set(got.languages) == ({"en"} | mac_codes) & CANARY_LANGUAGES
    # What Canary cannot decode is reported, not silently widened.
    assert set(got.dropped) == mac_codes - CANARY_LANGUAGES


def test_english_comes_first_even_when_the_mac_lists_it_later(mac, setting):
    setting("")
    mac(["pl-PL", "en-GB"])
    assert languages.resolve(CANARY_LANGUAGES, "Canary").languages == ("en", "pl")
    mac(["pl-PL"])
    assert languages.resolve(CANARY_LANGUAGES, "Canary").languages == ("en", "pl")


def test_unreadable_mac_languages_mean_english_only_and_say_so(
    tmp_path, monkeypatch, setting, caplog
):
    setting("")
    monkeypatch.setattr(languages, "_MAC_GLOBAL_PREFS", str(tmp_path / "missing.plist"))
    with caplog.at_level("WARNING", logger="whisper-studio"):
        first = languages.resolve(CANARY_LANGUAGES, "Canary")
        second = languages.resolve(CANARY_LANGUAGES, "Canary")
    assert first.languages == second.languages == ("en",)
    # Warned once, not once per utterance, and visible to Settings.
    assert caplog.text.count("automatic set is English only") == 1
    assert languages.summary()["mac_error"]


def test_explicit_setting_overrides_mac_languages(mac, setting, caplog):
    mac(["en-US", "pl-PL"])
    setting("de,fr")
    assert languages.resolve(CANARY_LANGUAGES, "Canary").languages == ("de", "fr")
    setting(" PL ")
    assert languages.resolve(CANARY_LANGUAGES, "Canary").languages == ("pl",)
    setting("pl,ja")
    got = languages.resolve(CANARY_LANGUAGES, "Canary")
    assert (got.languages, got.source, got.dropped) == (("pl",), "setting", ("ja",))
    # Nothing Canary decodes: a refusal naming the code, never the automatic
    # set standing in for the languages the user chose.
    setting("ja")
    caplog.clear()
    with caplog.at_level("WARNING", logger="whisper-studio"):
        for _ in range(3):
            with pytest.raises(languages.NoDecodableLanguage) as refused:
                languages.resolve(CANARY_LANGUAGES, "Canary")
    assert refused.value.dropped == ("ja",)
    assert caplog.text.count("cannot transcribe ja") == 1


def test_a_value_canary_cannot_decode_refuses_every_path(monkeypatch, mac, setting):
    """Transcripts, drafts and translation lines all refuse: nothing is
    decoded, detected or settled in a language the user did not name."""
    mac(["en-US", "pl-PL"])
    setting("ja")
    offered = _scripted_lid(monkeypatch, lambda s: ("en", 0.99))
    calls = _recording_generate(monkeypatch)
    session = canary_backend.create_session("S")
    assert _finals(session, 2.4, 0.9) == []
    session._buf.window = _pcm(2.0)
    session._last_interim_at = 0.0
    assert session.process(b"\x00" * 960) == []
    audio = np.zeros(SR, dtype=np.float32)
    assert canary_backend.translate_utterance(audio, None, target="en") == ""
    assert offered == [] and calls == []
    assert languages.tracker_for("S").settled is None
    info = languages.summary()
    assert info["canary"] == [] and info["dropped"]["canary"] == ["ja"]
    # Fixing the value applies to the next utterance of the same recording.
    setting("pl")
    assert [e["language"] for e in _finals(session, 0.9)] == ["pl"]


def test_mac_language_change_applies_without_a_restart(mac, setting):
    setting("")
    mac(["en-US"])
    assert languages.resolve(CANARY_LANGUAGES, "Canary").languages == ("en",)
    mac(["en-US", "pl-PL"])
    assert languages.resolve(CANARY_LANGUAGES, "Canary").languages == ("en", "pl")


# ── the tracker (pure) ─────────────────────────────────────────────────────

EN_PL = ("en", "pl")
LONG = languages.LID_MIN_SECONDS + 0.5
SHORT = languages.LID_MIN_SECONDS - 0.5
STRONG = 0.95
# Language ID torn between the two: the share it gives its pick barely beats
# the other candidate. (A low share for one of two languages is not "weak",
# it is the other language: "pl at 0.3" means 70% English.)
NEAR_EVEN = 0.55


def _said(code, share):
    return lambda: (code, share)


def _never():
    raise AssertionError("language ID must not run here")


def _settle(t, code, clips=6, seconds=3.0):
    """A take that has heard ``clips`` clear sentences in ``code``."""
    for _ in range(clips):
        t.commit(t.final(EN_PL, seconds, _said(code, 1.0)))
    assert t.settled == code


def test_a_take_settles_once_enough_speech_leans_one_way():
    t = languages.LanguageTracker()
    # One short clip is not enough speech to settle on, however sure.
    choice = t.final(EN_PL, SHORT, _said("pl", 1.0))
    assert (choice.language, choice.outcome) == ("pl", "unsettled")
    t.commit(choice)
    assert t.settled is None
    # More Polish adds up and settles it.
    choice = t.final(EN_PL, LONG, _said("pl", 0.8))
    assert choice.outcome == "settled"
    t.commit(choice)
    assert t.settled == "pl"


def test_an_even_split_never_settles():
    t = languages.LanguageTracker()
    for _ in range(3):
        choice = t.final(EN_PL, LONG, _said("pl", NEAR_EVEN))
        assert not choice.trusted
        t.commit(choice)
    assert t.settled is None


def test_one_confident_short_clip_cannot_flip_a_settled_take():
    """The field case (2026-09-29): a 1.8 s clip read as Polish at 100%
    switched an English take, and the next 8 s of English, read as English
    at 56%, was decoded as Polish."""
    t = languages.LanguageTracker()
    _settle(t, "en")
    blip = t.final(EN_PL, 1.83, _said("pl", 1.0))
    assert (blip.language, blip.outcome) == ("en", "kept")
    t.commit(blip)
    after = t.final(EN_PL, 8.0, _said("en", 0.56))
    assert (after.language, after.outcome) == ("en", "kept")
    t.commit(after)
    assert t.settled == "en"


def test_sustained_evidence_switches_the_take():
    t = languages.LanguageTracker()
    _settle(t, "en")
    outcomes = []
    for _ in range(12):
        choice = t.final(EN_PL, 3.0, _said("pl", STRONG))
        outcomes.append(choice.outcome)
        t.commit(choice)
        if t.settled == "pl":
            break
    assert t.settled == "pl"
    # Not on the first Polish sentence, and not after half a minute either.
    assert 2 <= len(outcomes) <= 8, outcomes
    assert outcomes[-1] == "switched"


def test_a_final_that_produced_no_text_never_moves_the_tracker():
    t = languages.LanguageTracker()
    choice = t.final(EN_PL, LONG, _said("pl", STRONG))
    assert choice.outcome == "settled"  # would settle, but nothing committed
    assert t.settled is None
    # Nor does its evidence count: the next weak clip starts from nothing.
    assert t.final(EN_PL, SHORT, _said("en", 1.0)).language == "en"


def test_a_new_candidate_list_unsettles_the_take():
    t = languages.LanguageTracker()
    t.commit(t.final(EN_PL, LONG, _said("pl", STRONG)))
    assert t.settled == "pl"
    choice = t.final(("en", "pl", "de"), SHORT, _said("de", 0.9))
    assert t.settled is None
    # Evidence from the old list does not carry over.
    assert choice.language == "de" and not choice.trusted


def test_language_id_failure_stays_inside_the_candidates():
    t = languages.LanguageTracker()
    choice = t.final(EN_PL, LONG, _said(None, 0.0))
    assert choice.language in EN_PL and not choice.trusted
    t.commit(choice)
    assert t.settled is None
    t.commit(t.final(EN_PL, LONG, _said("pl", STRONG)))
    choice = t.final(EN_PL, LONG, _said(None, 0.0))
    assert choice.language == "pl" and choice.trusted


def test_drafts_never_move_the_tracker():
    t = languages.LanguageTracker()
    # Too short to judge: no draft language, and no detection run.
    assert t.draft(EN_PL, SHORT, _never) is None
    strong = t.draft(EN_PL, LONG, _said("pl", STRONG))
    assert strong.language == "pl" and strong.trusted
    assert not t.draft(EN_PL, LONG, _said("pl", NEAR_EVEN)).trusted
    assert t.settled is None
    t.commit(t.final(EN_PL, LONG, _said("pl", STRONG)))
    assert t.draft(EN_PL, SHORT, _never).language == "pl"
    # A draft renders in whatever the final would decode, and moves nothing.
    for detected in (_said("en", STRONG), _said("pl", STRONG)):
        assert t.draft(EN_PL, LONG, detected).language == t.final(EN_PL, LONG, detected).language
    assert t.settled == "pl"


def test_unsure_clips_name_their_rivals_and_sure_ones_none():
    t = languages.LanguageTracker()
    _settle(t, "en")
    assert t.final(EN_PL, LONG, _said("en", 0.99)).rivals == ()
    # Weak, short or disagreeing clips are decoded in the other language too.
    assert t.final(EN_PL, LONG, _said("en", NEAR_EVEN)).rivals == ("pl",)
    assert t.final(EN_PL, SHORT, _said("en", 0.99)).rivals == ("pl",)
    assert t.final(EN_PL, 1.83, _said("pl", 1.0)).rivals == ("pl",)
    # A pinned language is never in doubt.
    assert t.final(("pl",), LONG, _never).rivals == ()


def test_a_single_candidate_pins_without_language_id():
    t = languages.LanguageTracker()
    assert t.final(("pl",), LONG, _never).language == "pl"
    assert t.draft(("pl",), SHORT, _never).language == "pl"


# ── CanarySession end to end (scripted LID and decode) ─────────────────────


def test_blank_setting_never_offers_ru_or_uk_to_language_id(monkeypatch, mac, setting):
    mac(["en-US", "pl-PL"])
    setting("")
    offered = _scripted_lid(monkeypatch, lambda s: ("pl", 0.99))
    _recording_generate(monkeypatch)
    session = canary_backend.create_session("S")
    _finals(session, 0.9, 2.4, 1.2, 5.0)
    session._buf.window = _pcm(2.0)
    session._last_interim_at = 0.0
    session.process(b"\x00" * 960)
    assert offered
    assert {frozenset(c) for c in offered} == {frozenset({"en", "pl"})}


def test_a_short_misdetection_at_the_start_does_not_poison_the_take(monkeypatch, mac, setting):
    """The reported cascade: a take opened with a short clip language ID got
    wrong ("Tak." heard as English, confidently) used to make that language
    sticky, so later Polish sentences came out as translations. Now an unsure
    clip is decoded in both languages and Canary's own confidence keeps the
    real one, and the take settles only once Polish evidence adds up."""
    mac(["en-US", "pl-PL"])
    setting("")
    answers = iter(
        [
            ("en", 0.99),  # 0.9 s "Tak.", misheard
            ("pl", 0.75),  # short Polish
            ("pl", 0.75),
            ("pl", 0.97),  # a long, clear Polish sentence
            ("en", 0.99),  # short Polish misheard again
        ]
    )
    _scripted_lid(monkeypatch, lambda s: next(answers))
    # The audio is Polish: Canary is more confident decoding it as Polish.
    calls = _recording_generate(monkeypatch, prefer=lambda langs: "pl")
    session = canary_backend.create_session("S")
    events = _finals(session, 0.9, 1.2, 0.8)
    assert all(src == tgt for src, tgt in calls)  # transcription, never translation
    assert [e["language"] for e in events] == ["pl", "pl", "pl"]
    assert calls.tie_breaks[0] == ("en", "pl")  # the misheard opener was in doubt
    assert languages.tracker_for("S").settled is None  # three short clips: not yet
    # A long clear sentence settles Polish, and a short misheard clip keeps it.
    events = _finals(session, 2.4, 0.9)
    assert [e["language"] for e in events] == ["pl", "pl"]
    assert languages.tracker_for("S").settled == "pl"


def test_an_empty_or_junk_final_never_settles_the_take(monkeypatch, mac, setting):
    mac(["en-US", "pl-PL"])
    setting("")
    _scripted_lid(monkeypatch, lambda s: ("pl", 0.99) if s >= 2 else ("en", 0.8))
    # The long clip is noise that decodes to nothing.
    calls = _recording_generate(monkeypatch, lambda lang, s: "" if s >= 2 else "ok")
    session = canary_backend.create_session("S")
    assert _finals(session, 3.0) == []
    _finals(session, 0.9)
    assert languages.tracker_for("S").settled is None
    assert calls[-1] == ("en", "en")  # the short clip's own pick, not the noise's


def test_default_english_only_mac_pins_canary_without_language_id(monkeypatch, setting):
    setting("")
    monkeypatch.setattr(lid, "detect", lambda a, c: (_ for _ in ()).throw(AssertionError))
    calls = _recording_generate(monkeypatch)
    session = canary_backend.create_session()
    events = _finals(session, 2.0)
    assert [e["language"] for e in events] == ["en"] and calls == [("en", "en")]


def test_a_settings_edit_applies_to_the_next_utterance(monkeypatch, mac, setting):
    mac(["en-US", "pl-PL"])
    setting("")
    offered = _scripted_lid(monkeypatch, lambda s: ("pl", 0.99))
    _recording_generate(monkeypatch)
    session = canary_backend.create_session("S")
    _finals(session, 2.0)
    assert languages.tracker_for("S").settled == "pl"
    setting("de,fr")
    offered = _scripted_lid(monkeypatch, lambda s: ("de", 0.8))
    events = _finals(session, 0.9)
    assert offered == [("de", "fr")]
    assert events[0]["language"] == "de"
    assert languages.tracker_for("S").settled is None


def test_polish_filler_is_kept_while_english_filler_and_loops_are_dropped(monkeypatch, setting):
    session = canary_backend.create_session()
    setting("pl")
    _recording_generate(monkeypatch, lambda lang, s: "No.")
    assert [e["text"] for e in _finals(session, 1.0)] == ["No."]
    _recording_generate(monkeypatch, lambda lang, s: "nie wiem nie wiem nie wiem nie wiem")
    assert _finals(session, 1.0) == []
    setting("en")
    _recording_generate(monkeypatch, lambda lang, s: "Thank you.")
    assert _finals(session, 1.0) == []


def test_translation_detects_a_parakeet_source_among_every_canary_language(monkeypatch, setting):
    """Parakeet lines carry no language. With a blank setting their source is
    detected over all of Canary's languages, not the transcription auto set:
    on the default English-only Mac that set would pin every source to
    English and silently skip every line to an English target."""
    setting("")
    offered = _scripted_lid(monkeypatch, lambda s: ("pl", 0.9))
    calls = _recording_generate(monkeypatch, lambda lang, s: "Good morning.")
    audio = np.zeros(SR, dtype=np.float32)
    assert canary_backend.translate_utterance(audio, None, target="en") == "Good morning."
    assert calls == [("pl", "en")]
    assert set(offered[0]) == CANARY_LANGUAGES and offered[0][0] == "en"
    # A source detected as the target itself is still skipped, and so is one
    # the classifier could not name (English heads the search).
    for answer in (("en", 0.9), (None, 0.0)):
        _scripted_lid(monkeypatch, lambda s, answer=answer: answer)
        assert canary_backend.translate_utterance(audio, None, target="en") == ""
    assert calls == [("pl", "en")]
    assert languages._trackers == {}  # translation belongs to no take


def test_translation_detects_its_source_within_a_typed_list(monkeypatch, setting):
    setting("pl,de")
    offered = _scripted_lid(monkeypatch, lambda s: ("de", 0.9))
    calls = _recording_generate(monkeypatch, lambda lang, s: "Good morning.")
    audio = np.zeros(SR, dtype=np.float32)
    assert canary_backend.translate_utterance(audio, None, target="en") == "Good morning."
    assert offered == [("pl", "de")] and calls == [("de", "en")]


def test_one_info_line_per_final_carries_the_language_evidence(monkeypatch, mac, setting, caplog):
    mac(["en-US", "pl-PL"])
    setting("")
    _scripted_lid(monkeypatch, lambda s: ("pl", 0.97))
    _recording_generate(monkeypatch)
    session = canary_backend.create_session()
    with caplog.at_level("INFO", logger="whisper-studio"):
        _finals(session, 2.4)
    lines = [r.getMessage() for r in caplog.records if "Canary utterance" in r.getMessage()]
    assert len(lines) == 1
    for fact in ("2.40 s", "en,pl", "pl 0.97", "settled", "decode pl"):
        assert fact in lines[0], (fact, lines[0])


# ── live drafts (scripted LID and decode) ──────────────────────────────────


def _draft(session, seconds):
    """One chunk with no utterance boundary and ``seconds`` in flight."""
    session._buf = _Utterances()
    session._buf.window = _pcm(seconds)
    session._last_interim_at = 0.0
    return [e for e in session.process(b"\x00" * 960) if e["kind"] == "interim"]


def test_an_unsettled_take_never_drafts_in_a_guessed_language(monkeypatch, mac, setting):
    """The reported symptom: a live draft rendered in a weakly detected
    language is a translation, which the final then replaces."""
    mac(["en-US", "pl-PL"])
    setting("")
    offered = _scripted_lid(monkeypatch, lambda s: ("en", NEAR_EVEN))
    calls = _recording_generate(monkeypatch)
    session = canary_backend.create_session("S")
    # Long enough to detect, but the detection is weak: no draft, no decode.
    assert _draft(session, LONG) == [] and calls == []
    assert len(offered) == 1
    # Too short to settle anything: neither language ID nor a decode runs.
    assert _draft(session, SHORT) == [] and calls == [] and len(offered) == 1
    # Once a final settles Polish, a short window drafts in Polish.
    _scripted_lid(monkeypatch, lambda s: ("pl", STRONG))
    _finals(session, LONG)
    assert languages.tracker_for("S").settled == "pl"
    del calls[:]
    assert [e["text"] for e in _draft(session, SHORT)] == ["<pl>"]
    assert calls == [("pl", "pl")]


def test_drafts_render_in_the_fallback_while_language_id_is_down(monkeypatch, mac, setting):
    """A classifier that cannot load leaves every final on the candidates'
    head, so drafts render in that same language rather than vanishing for
    the whole take."""
    mac(["en-US", "pl-PL"])
    setting("")
    _scripted_lid(monkeypatch, lambda s: (None, 0.0))
    calls = _recording_generate(monkeypatch)
    session = canary_backend.create_session("S")
    assert [e["text"] for e in _draft(session, LONG)] == ["<en>"]
    assert [e["language"] for e in _finals(session, LONG)] == ["en"]
    assert calls == [("en", "en"), ("en", "en")]
    assert languages.tracker_for("S").settled is None  # a fallback never settles


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
    audio = np.zeros(SR, dtype=np.float32)
    with caplog.at_level("WARNING", logger="whisper-studio"):
        for _ in range(5):  # a final and several drafts in a row
            assert lid.detect(audio, EN_PL) == (None, 0.0)
        assert len(attempts) == 1
        now[0] += lid._LOAD_RETRY_SECONDS
        assert lid.detect(audio, EN_PL) == (None, 0.0)
    assert len(attempts) == 2
    assert caplog.text.count("Language detection failed") == 2


# ── the websocket keeps the take's language across a reconnect ─────────────


@pytest.fixture
def ws_client(monkeypatch, mac, setting):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server import websocket as ws_mod

    mac(["en-US", "pl-PL"])
    setting("")

    def lid_script(seconds):
        return ("pl", 0.97) if seconds >= languages.LID_MIN_SECONDS else ("en", 0.99)

    _scripted_lid(monkeypatch, lid_script)
    _recording_generate(monkeypatch)

    class OneShot(_Utterances):
        def feed(self, chunk):
            return [chunk]

    monkeypatch.setattr(canary_backend, "UtteranceBuffer", OneShot)
    monkeypatch.setattr(canary_backend, "is_loaded", lambda: True)
    monkeypatch.setattr(canary_backend, "unload", lambda: None)
    conf = {
        "transcription_backend": "canary",
        "translate_mode": "off",
        "speaker_split_turns": False,
    }
    monkeypatch.setattr(ws_mod, "config_get", lambda k: conf.get(k))
    monkeypatch.setattr(ws_mod.diarization, "get_session", lambda sid: None)
    monkeypatch.setattr(ws_mod.diarization, "drop_session", lambda sid: None)
    app = FastAPI()
    app.include_router(ws_mod.router)
    return TestClient(app)


def _say(ws, seconds):
    ws.send_bytes(_pcm(seconds))
    msg = ws.receive_json()
    while msg["type"] != "transcript":
        msg = ws.receive_json()
    return msg["language"]


def _stop(ws):
    ws.send_json({"type": "stop"})
    while ws.receive_json()["type"] != "session_ended":
        pass


def test_a_reconnect_keeps_the_settled_language(ws_client):
    with ws_client.websocket_connect("/ws?session_id=R1") as ws:
        settled = _say(ws, 3.0)
    # Dropped without a stop (the watchdog reconnects with the same id).
    with ws_client.websocket_connect("/ws?session_id=R1") as ws:
        assert _say(ws, 0.9) == settled


def test_a_live_switch_back_to_canary_keeps_the_settled_language(ws_client):
    with ws_client.websocket_connect("/ws?session_id=R2") as ws:
        settled = _say(ws, 3.0)
        ws.send_json({"type": "set_model", "backend": "streaming"})
        ws.send_json({"type": "set_model", "backend": "canary"})
        assert _say(ws, 0.9) == settled


def test_an_explicit_stop_starts_the_next_take_unsettled(ws_client):
    with ws_client.websocket_connect("/ws?session_id=R3") as ws:
        settled = _say(ws, 3.0)
        _stop(ws)
    with ws_client.websocket_connect("/ws?session_id=R3") as ws:
        # The short clip decodes with its own pick: nothing carried over.
        assert _say(ws, 0.9) != settled


def test_a_take_that_opens_while_the_last_one_drains_starts_unsettled(ws_client, monkeypatch):
    """The client opens the next take's socket as soon as it sends stop,
    while this one may still be flushing its tail and draining translations.
    A session created in that window must get a fresh tracker, and keep it
    once the old take's stop completes; the flush itself still sees the
    settled language."""
    seen = {}
    real_finish = canary_backend.CanarySession.finish

    def finish(self):
        seen["next"] = languages.tracker_for("R5")  # the next take's session
        seen["own"] = self._languages
        return real_finish(self)

    monkeypatch.setattr(canary_backend.CanarySession, "finish", finish)
    with ws_client.websocket_connect("/ws?session_id=R5") as ws:
        settled = _say(ws, 3.0)
        _stop(ws)
    assert seen["own"].settled == settled
    assert seen["next"] is not seen["own"] and seen["next"].settled is None
    # A watchdog reconnect of the new take shares the tracker it started with.
    assert languages.tracker_for("R5") is seen["next"]


def test_a_new_take_starts_unsettled_when_the_last_stop_never_arrived(ws_client):
    """Stop pressed while the socket was down (or a reload mid-recording)
    never reaches the server; the next press of Record is a new take and
    must not inherit the old take's language."""
    with ws_client.websocket_connect("/ws?session_id=T1&take=A") as ws:
        settled = _say(ws, 3.0)
    # A watchdog reconnect of the same take keeps it.
    with ws_client.websocket_connect("/ws?session_id=T1&take=A") as ws:
        assert _say(ws, 0.9) == settled
    with ws_client.websocket_connect("/ws?session_id=T1&take=B") as ws:
        assert _say(ws, 0.9) != settled


def test_a_late_stop_of_the_last_take_keeps_the_next_takes_language():
    old = languages.tracker_for("T2", "A")
    new = languages.tracker_for("T2", "B")
    assert new is not old
    languages.forget_tracker("T2", "A")
    assert languages.tracker_for("T2", "B") is new
    languages.forget_tracker("T2", "B")
    assert languages.tracker_for("T2", "B") is not new


def test_dictation_never_shares_the_recording_language(ws_client):
    with ws_client.websocket_connect("/ws?session_id=R4") as ws:
        settled = _say(ws, 3.0)
    with ws_client.websocket_connect("/ws?session_id=R4&dictation=1&backend=canary") as ws:
        assert _say(ws, 0.9) != settled
    with ws_client.websocket_connect("/ws?session_id=R4") as ws:
        assert _say(ws, 0.9) == settled


# ── GET /api/config exposes the effective set, read-only ───────────────────


def test_config_endpoint_reports_the_effective_languages(tmp_path, monkeypatch, mac):
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.infrastructure.config as cfg

    user = tmp_path / "config.user.json"
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", str(user))
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(tmp_path / "config.json"))
    mac(["en-US", "pl-PL", "ckb-PL"])
    app = FastAPI()
    app.include_router(cfg.router)
    client = TestClient(app)

    def fetch(raw):
        user.write_text(json.dumps({"whisper_language": raw}))
        cfg._invalidate_cache()
        return client.get("/api/config").json()["_transcription_languages"]

    blank = fetch("")
    assert blank["auto"] == languages.auto_languages(CANARY_LANGUAGES)
    assert blank["canary"] == blank["auto"] and blank["canary_source"] == "auto"
    assert blank["whisper"] == []  # Whisper detects the language itself
    assert "ckb" in blank["dropped"]["canary"]
    typed = fetch("pl,ja")
    assert typed["canary"] == ["pl"] and typed["dropped"]["canary"] == ["ja"]
    assert typed["whisper"] == ["pl", "ja"]
    assert typed["auto"] == blank["auto"]  # the blank meaning is still shown
    # A value Canary refuses reads as an empty set, not the automatic one.
    refused = fetch("ja")
    assert refused["canary"] == [] and refused["dropped"]["canary"] == ["ja"]
    assert refused["whisper"] == ["ja"]
    # The field is a view, not a setting: PUT never persists it.
    client.put("/api/config", json={"_transcription_languages": {"auto": ["xx"]}})
    assert "_transcription_languages" not in json.loads(user.read_text())
    cfg._invalidate_cache()
