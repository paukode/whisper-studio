"""Canary ASR backend: NVIDIA Canary-1B-v2 via vendored MLX code.

Transcribes 25 European languages AND translates any of them to English with
a real speech-translation head (unlike Whisper checkpoints without one, whose
translate task is silently ignored). Emits a live word-by-word draft like
the Parakeet backend (the in-flight utterance is re-decoded on every audio
chunk, steady-state decode ~0.4 s, under the ~1 s chunk cadence), with the
clean, settled final at each silence boundary.

One Canary limitation shapes this module: the model has NO language
head. Every decode names a source and a target, and Canary follows the
target token rather than the audio, so a wrong pick comes out as a fluent
translation into that language. A small on-CPU language-ID classifier
(server/asr/lid.py) picks the language per utterance among the
transcription languages (server/asr/languages.py: the setting, or English
plus the Mac's preferred languages when it is blank), and a per-take
tracker only settles on strong evidence. A single candidate skips detection
entirely (pinned language, zero overhead).

Model load is lazy (first session); importing this module is cheap.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from server.asr import languages
from server.audio_buffer import UtteranceBuffer
from server.infrastructure.paths import models_root

log = logging.getLogger("whisper-studio")

MODELS_DIR = models_root()
CANARY_MODEL_DIR = os.path.join(MODELS_DIR, "canary-1b-v2")
SAMPLE_RATE = 16000

# Don't run an interim decode until the in-flight utterance carries at least
# this much audio: below ~0.5 s the draft is empty or one unreliable
# fragment, not worth the decode or the UI flicker (same idea as the
# Parakeet backend).
_MIN_INTERIM_SECONDS = 0.5
_MIN_INTERIM_BYTES = int(_MIN_INTERIM_SECONDS * SAMPLE_RATE) * 2  # PCM16

# ── Draft scheduling ─────────────────────────────────────────────────────────
# Drafts, finals, and translations share this module's single decode thread,
# and a full-context draft re-decode costs ~0.4 s. Drafts are cosmetic, so
# they yield: a draft is skipped outright while any translation is queued
# (the orchestrator counts them in via note_translation_queued), and the
# draft cadence stretches as the in-flight window grows (each re-decode gets
# more expensive with window length).
_pending_translations = 0
_pending_lock = threading.Lock()
# Seconds between drafts: base cadence, and the relaxed cadence once the
# in-flight window passes _INTERIM_SLOW_AFTER_S of audio.
_INTERIM_INTERVAL_S = 0.9
_INTERIM_SLOW_INTERVAL_S = 2.0
_INTERIM_SLOW_AFTER_S = 4.0


def note_translation_queued() -> None:
    """The orchestrator queued a translate_utterance on this executor."""
    global _pending_translations
    with _pending_lock:
        _pending_translations += 1


def _note_translation_done() -> None:
    global _pending_translations
    with _pending_lock:
        _pending_translations = max(0, _pending_translations - 1)


def _translations_waiting() -> bool:
    return _pending_translations > 0


CANARY_REPO_ID = "qfuxa/canary-mlx"

# Languages Canary-1B-v2 transcribes (and translates to/from English).
CANARY_LANGUAGES = {
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu",
    "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru",
    "uk",
}  # fmt: skip

# Single worker: MLX evaluation streams are thread-local, so the model must
# load and decode on one thread (same constraint as the Parakeet backend).
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="canary-asr")

_model = None
_model_lock = threading.Lock()


def _ensure_model() -> str:
    """Download the Canary MLX weights if not already present (idempotent)."""
    weight_file = os.path.join(CANARY_MODEL_DIR, "model.safetensors")
    if not os.path.exists(weight_file):
        from huggingface_hub import snapshot_download

        log.info("Downloading Canary model %s ...", CANARY_REPO_ID)
        snapshot_download(
            repo_id=CANARY_REPO_ID,
            local_dir=CANARY_MODEL_DIR,
            local_dir_use_symlinks=False,
        )
        log.info("Canary model download complete.")
    return os.path.abspath(CANARY_MODEL_DIR)


def is_loaded() -> bool:
    return _model is not None


def load() -> None:
    """Load the model into memory (mirrors mlx-audio's base_load_model steps,
    minus its quantization/remapping branches the fp16 repo never hits). Run
    on ``executor``."""
    global _model
    if _model is not None:
        return
    with _model_lock:
        if _model is not None:
            return
        from server.asr.canary import Model, ModelConfig

        model_dir = Path(_ensure_model())
        import mlx.core as mx

        log.info("Loading Canary model from %s ...", model_dir)
        config = json.loads((model_dir / "config.json").read_text())
        config["model_path"] = str(model_dir)
        model = Model(ModelConfig.from_dict(config))
        weights = mx.load(str(model_dir / "model.safetensors"))
        if hasattr(model, "sanitize"):
            weights = model.sanitize(weights)
        model.load_weights(list(weights.items()), strict=False)
        mx.eval(model.parameters())
        model.eval()
        # Loads the SentencePiece tokenizer from the model dir.
        _model = Model.post_load_hook(model, model_dir)
        log.info("Canary model loaded.")


def preload() -> None:
    """Eager startup warmup on the decode thread (best-effort)."""
    executor.submit(load).result()


def unload() -> None:
    """Release the weights (local mode, engine switch)."""
    global _model
    _model = None
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception as e:
        log.debug("Canary mx cache clear on unload failed: %s", e)
    import gc

    gc.collect()
    log.info("Canary model unloaded.")


def _candidates() -> tuple[str, ...]:
    """The languages Canary may decode with for the next utterance, read per
    utterance so a Settings edit applies to a live recording. Raises
    languages.NoDecodableLanguage when the setting names nothing Canary
    decodes: Canary then refuses rather than decode with other languages."""
    return languages.resolve(CANARY_LANGUAGES, "Canary").languages


def _lid(audio: np.ndarray, candidates: tuple[str, ...]) -> tuple[str | None, float]:
    """Language ID over ``candidates`` (resolved at call time, so tests and
    callers see a patched ``lid.detect``)."""
    from server.asr import lid

    return lid.detect(audio, candidates)


def _is_junk(text: str, language: str) -> bool:
    """Whether a decode is noise rather than speech.

    Whisper's phrase blocklist is English filler plus Whisper's own
    YouTube-outro artifacts, so it only judges English output: on any other
    language it would drop real words (Polish "No." is "well"). Every
    language keeps the language-neutral loop check.
    """
    from server.asr import whisper_backend

    if language == "en":
        return whisper_backend._is_junk(text)
    return whisper_backend.is_repetition_hallucination(text)


def _generate(audio: np.ndarray, source_lang: str, target_lang: str) -> str:
    load()
    result = _model.generate(audio, source_lang=source_lang, target_lang=target_lang)
    return (result.text or "").strip()


def translate_utterance(
    audio_data: np.ndarray, language: str | None = None, target: str = "en"
) -> str:
    """Translation of one utterance via Canary's native AST head.

    Canary is the app's universal model translator: this runs on Canary's
    own executor regardless of which engine transcribed the audio, so
    Whisper and Parakeet sessions can translate through it too. ``language``
    is the transcribing engine's language ID when it has one; utterances
    from engines without language ID (Parakeet) are detected here, within a
    typed Transcription Languages list, or among all of Canary's languages
    when it is blank (languages.translation_sources says why).

    Canary translates bidirectionally with ENGLISH AS THE HUB: any of its 25
    languages → English, and English → any of them, never X → Y with both
    non-English. An unsupported pair (or same-language input) returns "" so
    the client's pending slot clears without a bogus line.

    Pairs with note_translation_queued(): the queued-translation count is
    what makes live drafts yield this thread, so it decrements on entry
    (every exit path counts as done).
    """
    _note_translation_done()
    from server.asr.whisper_backend import is_repetition_hallucination

    source = language
    if source not in CANARY_LANGUAGES:
        # Stateless: an engine without language ID (Parakeet) sent this, so
        # detect the source here, without touching any take's tracker.
        try:
            candidates = languages.translation_sources(CANARY_LANGUAGES, "Canary")
        except languages.NoDecodableLanguage:
            return ""  # resolve logged the reason; Settings shows it
        duration = len(audio_data) / SAMPLE_RATE
        source = languages.pick_once(
            candidates, duration, lambda: _lid(audio_data, candidates)
        ).language
    if source == target:
        return ""
    if target not in CANARY_LANGUAGES or (source != "en" and target != "en"):
        log.warning("Canary: unsupported translation pair %s->%s, skipped", source, target)
        return ""
    text = ""
    try:
        text = _generate(audio_data, source_lang=source, target_lang=target)
        # Only the loop check: the source already passed its own engine's
        # transcript filter, so a short phrase here ("Thank you." for
        # "Dziekuje.") is a real translation, not a silence hallucination.
        if text and is_repetition_hallucination(text):
            text = ""
    except Exception as e:
        log.warning("Canary translation error: %s", e)
    return text


def _decode_utterance(
    utterance_pcm: bytes, tracker: languages.LanguageTracker
) -> tuple[str, np.ndarray, str | None]:
    """PCM16 utterance -> (filtered text, float32 audio, decode language).

    The tracker picks the language and is updated only when the decode
    produced text, so noise, junk or a failed decode never settles or
    switches the take's language. Logs one INFO line per utterance with the
    evidence behind the language, which is what makes a wrong-language
    report diagnosable. A setting that names nothing Canary decodes yields
    no text and no language: Canary refuses instead of guessing.
    """
    # No energy gate: the VAD is the only speech filter (matching Parakeet;
    # RMS gates silently ate quiet mics, proven live).
    audio = np.frombuffer(utterance_pcm, dtype=np.int16).astype(np.float32) / 32768.0
    duration = len(audio) / SAMPLE_RATE

    try:
        candidates = _candidates()
    except languages.NoDecodableLanguage as e:
        log.info("Canary utterance: %.2f s, not decoded: %s", duration, e)
        return "", audio, None
    choice = tracker.final(candidates, duration, lambda: _lid(audio, candidates))
    text = ""
    try:
        text = _generate(audio, source_lang=choice.language, target_lang=choice.language)
        if text and _is_junk(text, choice.language):
            log.debug("Canary: hallucination filter dropped %r", text[:80])
            text = ""
    except Exception as e:
        log.warning("Canary transcription error: %s", e)
    if text:
        tracker.commit(choice)
    log.info("Canary utterance: %s%s", choice.describe(), "" if text else ", no text")
    return text, audio, choice.language


class CanarySession:
    """One per-connection decoder: live interim drafts plus settled finals.

    The language lives in the take's tracker (languages.tracker_for), not on
    this object, so a watchdog reconnect or a live engine switch within one
    take keeps the settled language; the websocket forgets it on an explicit
    stop."""

    def __init__(self, session_id: str | None = None, take: str = "") -> None:
        self._buf = UtteranceBuffer()
        self._languages = languages.tracker_for(session_id, take)
        self._last_interim = ""
        self._last_interim_at = 0.0

    def _decode(self, utterance_pcm: bytes) -> tuple[str, np.ndarray, str | None]:
        return _decode_utterance(utterance_pcm, self._languages)

    def process(self, raw_pcm: bytes) -> list[dict]:
        events: list[dict] = []
        completed = self._buf.feed(raw_pcm)
        if completed:
            for utterance_pcm in completed:
                text, audio, language = self._decode(utterance_pcm)
                if text:
                    events.append(
                        {"kind": "final", "text": text, "audio": audio, "language": language}
                    )
            # An utterance just closed; the next interim starts a fresh window.
            return self._close_draft(events)

        # No boundary this chunk: re-decode the growing in-flight utterance
        # as a volatile draft (mirrors the Parakeet backend). The draft only
        # renders in a language the final would also use (pinned, settled,
        # what a strong detection of this window would settle or switch to,
        # or the candidates' head while the classifier is down), so a guessed
        # language never shows as a live translation. Drafts never move the
        # tracker; only finals do.
        pending = self._buf.pending()
        if self._last_interim and not pending:
            # The VAD discarded the utterance the draft belonged to (too
            # little voiced audio): it closed without a final.
            return self._close_draft(events)
        if len(pending) >= _MIN_INTERIM_BYTES and self._draft_due(len(pending)):
            audio = np.frombuffer(pending, dtype=np.int16).astype(np.float32) / 32768.0
            try:
                candidates = _candidates()
            except languages.NoDecodableLanguage:
                return events  # Canary refuses; the final logs why
            choice = self._languages.draft(
                candidates, len(audio) / SAMPLE_RATE, lambda: _lid(audio, candidates)
            )
            if choice is None:
                return events
            if not choice.drafts:
                # Detection ran and was weak: wait a draft interval before
                # spending the thread on it again.
                self._last_interim_at = time.monotonic()
                return events
            language = choice.language
            try:
                text = _generate(audio, source_lang=language, target_lang=language)
            except Exception as e:
                log.debug("Canary interim decode failed: %s", e)
                text = ""
            self._last_interim_at = time.monotonic()
            if text and text != self._last_interim:
                self._last_interim = text
                events.append({"kind": "interim", "text": text})
        return events

    def _draft_due(self, pending_bytes: int) -> bool:
        """Whether to spend this thread on a cosmetic draft right now."""
        if _translations_waiting():
            return False
        window_s = pending_bytes / 2 / SAMPLE_RATE
        interval = (
            _INTERIM_SLOW_INTERVAL_S if window_s > _INTERIM_SLOW_AFTER_S else _INTERIM_INTERVAL_S
        )
        return (time.monotonic() - self._last_interim_at) >= interval

    def _close_draft(self, events: list[dict]) -> list[dict]:
        """End the in-flight utterance's draft. When the utterance produced
        no final (junk-filtered, empty decode, or too little voiced audio for
        the VAD), an empty interim withdraws the draft still on screen."""
        if self._last_interim and not events:
            events.append({"kind": "interim", "text": ""})
        self._last_interim = ""
        return events

    def finish(self) -> list[dict]:
        events: list[dict] = []
        try:
            tail = self._buf.flush()
            if tail is not None:
                text, audio, language = self._decode(tail)
                if text:
                    events.append(
                        {"kind": "final", "text": text, "audio": audio, "language": language}
                    )
        except Exception as e:
            log.debug("Canary finish flush failed: %s", e)
        return self._close_draft(events)

    def close(self) -> None:
        pass


def create_session(session_id: str | None = None, take: str = "") -> CanarySession:
    """A decoder for one connection; ``session_id`` (the chat session the
    recording belongs to) and ``take`` (the recording's take id) select the
    language tracker it shares with a reconnect of the same take."""
    return CanarySession(session_id, take)
