"""Transcription languages: which codes an engine decodes, and the per-take
language tracker Canary settles on.

One setting, ``whisper_language`` (labelled "Transcription Languages" in
Settings), drives this. What it means per engine:

- Canary: blank means English plus the Mac's preferred languages (System
  Settings > General > Language & Region) that Canary decodes, English
  first. One code pins the language; a comma list limits detection to it.
  A value that names nothing Canary decodes makes Canary refuse to
  transcribe (with the reason in Settings), never fall back to other
  languages.
- Whisper: blank keeps Whisper's own detection over every language it knows
  (server/asr/whisper_backend.py owns that path).
- Parakeet has no language input at all.

Why Canary needs a small, honest candidate set: it has no language head, so
every decode names a source and a target, and it follows the target token
rather than the audio. A wrong pick is therefore never a garbled transcript,
it is a fluent translation into the wrong language (Polish decoded as ``ru``
comes out as Russian). So the candidates never include a language the user
did not choose or does not have on their Mac, and the tracker only settles
on strong evidence.

Everything here is resolved per utterance (a Settings edit applies to a live
recording), and the Mac preferences file is only re-parsed when its mtime
changes.
"""

from __future__ import annotations

import logging
import os
import plistlib
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass

log = logging.getLogger("whisper-studio")

_MAC_GLOBAL_PREFS = os.path.expanduser("~/Library/Preferences/.GlobalPreferences.plist")

# (path, mtime_ns, base codes) of the last successful read.
_mac_cache: tuple[str, int, tuple[str, ...]] | None = None
_mac_error: str | None = None
_mac_lock = threading.Lock()

# Warnings that would otherwise repeat on every utterance are logged once per
# distinct cause (a new setting or a new failure logs again).
_warned: set[tuple] = set()
_warned_lock = threading.Lock()


def _warn_once(key: tuple, message: str, *args) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(message, *args)


def _base_code(tag: object) -> str | None:
    """'en-US' -> 'en', 'zh-Hans-CN' -> 'zh', 'ckb-PL' -> 'ckb'."""
    if not isinstance(tag, str):
        return None
    code = tag.replace("_", "-").split("-")[0].strip().lower()
    return code or None


def mac_languages() -> tuple[str, ...]:
    """The Mac's preferred languages as base codes, in the Mac's order.

    Empty when the preferences cannot be read; the reason is logged once and
    kept for ``summary()`` so Settings can say why the auto set is English
    only.
    """
    global _mac_cache, _mac_error
    path = _MAC_GLOBAL_PREFS
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError as e:
        return _mac_failed(path, f"cannot read {path}: {e.strerror or e}")
    with _mac_lock:
        if _mac_cache is not None and _mac_cache[:2] == (path, mtime):
            return _mac_cache[2]
    try:
        with open(path, "rb") as f:
            prefs = plistlib.load(f)
    except Exception as e:  # noqa: BLE001 (any unreadable plist means no Mac languages)
        return _mac_failed(path, f"cannot parse {path}: {e}")
    tags = prefs.get("AppleLanguages") if isinstance(prefs, dict) else None
    codes: list[str] = []
    for tag in tags if isinstance(tags, list) else []:
        code = _base_code(tag)
        if code and code not in codes:
            codes.append(code)
    with _mac_lock:
        _mac_cache = (path, mtime, tuple(codes))
        _mac_error = None
    return tuple(codes)


def _mac_failed(path: str, reason: str) -> tuple[str, ...]:
    global _mac_error
    with _mac_lock:
        _mac_error = reason
    _warn_once(
        ("mac", path, reason),
        "Transcription languages: %s; the automatic set is English only",
        reason,
    )
    return ()


def auto_languages(supported: Collection[str]) -> list[str]:
    """English plus the Mac's preferred languages that ``supported`` decodes,
    English first, de-duplicated. This is what a blank setting means for
    Canary."""
    out = ["en"] if "en" in supported else []
    for code in mac_languages():
        if code in supported and code not in out:
            out.append(code)
    return out


def configured_codes() -> list[str]:
    """The codes typed into the setting, parsed, in order (may be empty)."""
    from server.asr.whisper_backend import _parse_languages
    from server.infrastructure.config import get as config_get

    return list(dict.fromkeys(_parse_languages(config_get("whisper_language"))))


@dataclass(frozen=True)
class Candidates:
    """What an engine decodes with right now."""

    languages: tuple[str, ...]  # never empty; one entry pins the language
    source: str  # "setting" (typed codes) or "auto" (English plus the Mac's)
    dropped: tuple[str, ...]  # codes the engine cannot decode, left out


class NoDecodableLanguage(ValueError):
    """The setting names languages, none of which the engine decodes.

    The engine refuses instead of substituting the automatic set: the user
    said which languages are spoken, and decoding that speech as another
    language would turn it into a translation. The message is the reason
    shown to the user (Settings, and the log)."""

    def __init__(self, engine: str, dropped: tuple[str, ...]) -> None:
        self.engine = engine
        self.dropped = dropped
        super().__init__(
            f"{engine} cannot transcribe {', '.join(dropped)} from Transcription "
            f"Languages, so it does not transcribe; add a language {engine} "
            "transcribes, or clear the field for English plus the Mac's languages"
        )


def resolve(supported: Collection[str], engine: str) -> Candidates:
    """The candidate languages for ``engine`` (whose decodable codes are
    ``supported``): the typed setting, or the automatic set when it is
    blank. Codes the engine cannot decode are reported in ``dropped`` (and
    logged once), never silently widened into open detection. Raises
    ``NoDecodableLanguage`` when the setting names nothing the engine
    decodes."""
    typed = configured_codes()
    if typed:
        kept = tuple(c for c in typed if c in supported)
        dropped = tuple(c for c in typed if c not in supported)
        if not kept:
            refusal = NoDecodableLanguage(engine, dropped)
            _warn_once((engine, tuple(typed)), "%s", refusal)
            raise refusal
        if dropped:
            _warn_once(
                (engine, tuple(typed)),
                "%s cannot transcribe %s from Transcription Languages; left out",
                engine,
                ", ".join(dropped),
            )
        return Candidates(kept, "setting", dropped)
    dropped = tuple(c for c in mac_languages() if c not in supported)
    return Candidates(tuple(auto_languages(supported)), "auto", dropped)


def translation_sources(supported: Collection[str], engine: str) -> tuple[str, ...]:
    """Where ``engine`` looks for the source language of a line whose own
    engine named none (Parakeet), to translate it.

    A typed setting limits it to that list, as for transcription. A blank
    setting searches every language the engine decodes, English first, and
    not just the automatic set: here the output language is the user's
    translation target whatever source is picked, so a wrong pick can only
    skip a line (it equals the target, or the pair is unsupported), never
    turn it into another language. The automatic set would do worse: on an
    English-only Mac it pins every source to English, so every line to an
    English target would be skipped. Raises ``NoDecodableLanguage`` like
    ``resolve``."""
    if configured_codes():
        return resolve(supported, engine).languages
    rest = sorted(c for c in supported if c != "en")
    return ("en", *rest) if "en" in supported else tuple(rest)


def summary() -> dict:
    """The read-only ``_transcription_languages`` field of GET /api/config:
    what a blank setting resolves to, what each engine uses for the current
    value, and what was left out, so Settings can show the effective set."""
    from server.asr.canary_backend import CANARY_LANGUAGES
    from server.asr.whisper_backend import _KNOWN_WHISPER_LANGUAGES

    mac = mac_languages()
    with _mac_lock:
        mac_error = _mac_error
    try:
        canary = resolve(CANARY_LANGUAGES, "Canary")
        canary_languages, canary_source, canary_dropped = (
            list(canary.languages),
            canary.source,
            list(canary.dropped),
        )
    except NoDecodableLanguage as e:
        # Canary refuses this value: an empty list is what Settings shows.
        canary_languages, canary_source, canary_dropped = [], "setting", list(e.dropped)
    typed = configured_codes()
    return {
        "auto": auto_languages(CANARY_LANGUAGES),
        "mac": list(mac),
        "mac_error": mac_error,
        # Empty when the value names nothing Canary decodes (it refuses).
        "canary": canary_languages,
        "canary_source": canary_source,
        # Empty means Whisper detects the language itself, over every
        # language it knows (the meaning of a blank setting for Whisper).
        "whisper": [c for c in typed if c in _KNOWN_WHISPER_LANGUAGES],
        "dropped": {
            "canary": canary_dropped,
            "whisper": [c for c in typed if c not in _KNOWN_WHISPER_LANGUAGES],
        },
    }


# ── Per-take language tracker ────────────────────────────────────────────
# VoxLingua on short, noisy or accented clips is often confidently wrong, so
# a detection only settles the take's language on a long enough clip with a
# clear share of the candidate mass, and moving AWAY from a settled language
# needs much stronger evidence than settling did. Everything weaker inherits
# the settled language, or (before anything settles) decodes that one
# utterance with its own pick and leaves the take unsettled.
LID_MIN_SECONDS = 1.5
SETTLE_SHARE = 0.6
SWITCH_SHARE = 0.9

# Outcomes a Choice can carry. The first five are trusted languages; the
# last two are one-utterance guesses that never settle anything.
_TRUSTED = ("pinned", "settled", "switched", "kept", "inherited")


@dataclass(frozen=True)
class Choice:
    """The decode language for one utterance and how it was reached."""

    language: str
    candidates: tuple[str, ...]
    duration: float
    outcome: str  # pinned | settled | switched | kept | inherited | unsettled | lid-failed
    detected: str | None = None
    share: float = 0.0

    @property
    def trusted(self) -> bool:
        """Whether the language is known rather than guessed for this clip."""
        return self.outcome in _TRUSTED

    @property
    def drafts(self) -> bool:
        """Whether a live draft may render in this language: a trusted one,
        or the head of the candidates when the classifier is down (every
        final decodes with that same head while it stays down, so the draft
        never turns into a different language). Never a weak guess, which
        the final may not share."""
        return self.trusted or self.outcome == "lid-failed"

    def describe(self) -> str:
        lid = "lid skipped"
        if self.outcome != "pinned":
            lid = f"lid {self.detected} {self.share:.2f}" if self.detected else "lid failed"
        return (
            f"{self.duration:.2f} s, candidates {','.join(self.candidates)}, {lid}, "
            f"{self.outcome}, decode {self.language}"
        )


class LanguageTracker:
    """The settled language of one take (kept across a reconnect of the same
    take, see ``tracker_for``).

    ``draft`` only reads. ``final`` only notes a changed candidate list
    (which unsettles the take); settling or switching happens in ``commit``,
    called after a final produced text, so drafts, empty decodes and junk
    never move the settled language. Used from one decode thread only
    (Canary's single-worker executor).
    """

    def __init__(self) -> None:
        self._settled: str | None = None
        self._candidates: tuple[str, ...] = ()

    @property
    def settled(self) -> str | None:
        return self._settled

    def _settled_within(self, candidates: tuple[str, ...]) -> str | None:
        return self._settled if candidates == self._candidates else None

    def _observe(self, candidates: tuple[str, ...]) -> None:
        if candidates == self._candidates:
            return
        if self._candidates:
            log.info(
                "Canary languages changed to %s (was %s); the take's language is unsettled",
                ",".join(candidates),
                ",".join(self._candidates),
            )
        else:
            log.info("Canary languages for this take: %s", ",".join(candidates))
        self._candidates = candidates
        self._settled = None

    def _decide(
        self, candidates: tuple[str, ...], duration: float, detected: str | None, share: float
    ) -> Choice:
        settled = self._settled_within(candidates)

        def choice(language: str, outcome: str) -> Choice:
            return Choice(language, candidates, duration, outcome, detected, share)

        if detected not in candidates:  # the classifier failed (lid logs why)
            return choice(settled, "inherited") if settled else choice(candidates[0], "lid-failed")
        long_enough = duration >= LID_MIN_SECONDS
        if settled is None:
            if long_enough and share >= SETTLE_SHARE:
                return choice(detected, "settled")
            return choice(detected, "unsettled")
        if detected == settled:
            return choice(settled, "kept")
        if long_enough and share >= SWITCH_SHARE:
            return choice(detected, "switched")
        return choice(settled, "inherited")

    def final(
        self,
        candidates: tuple[str, ...],
        duration: float,
        detect: Callable[[], tuple[str | None, float]],
    ) -> Choice:
        """Decode language for a settled utterance. ``detect`` runs language
        ID over ``candidates`` (skipped when one candidate pins it)."""
        self._observe(candidates)
        if len(candidates) == 1:
            return Choice(candidates[0], candidates, duration, "pinned")
        detected, share = detect()
        return self._decide(candidates, duration, detected, share)

    def draft(
        self,
        candidates: tuple[str, ...],
        duration: float,
        detect: Callable[[], tuple[str | None, float]],
    ) -> Choice | None:
        """Language for a live draft of the in-flight window, never moving the
        tracker. None (and no detection run) while the window is too short to
        settle or switch anything and nothing is settled; the caller shows a
        draft only when the choice ``drafts``, so a guessed language is never
        rendered as a live translation.

        A settled take still runs detection on a long enough window: when the
        speaker has switched language, the draft follows what the final would
        switch to instead of showing a live translation into the old one."""
        if len(candidates) == 1:
            return Choice(candidates[0], candidates, duration, "pinned")
        settled = self._settled_within(candidates)
        if duration < LID_MIN_SECONDS:
            if settled is None:
                return None
            return Choice(settled, candidates, duration, "inherited")
        detected, share = detect()
        return self._decide(candidates, duration, detected, share)

    def commit(self, choice: Choice) -> None:
        """Record a final that produced text: settle or switch when its choice
        says so."""
        if choice.candidates == self._candidates and choice.outcome in ("settled", "switched"):
            self._settled = choice.language


def pick_once(
    candidates: tuple[str, ...],
    duration: float,
    detect: Callable[[], tuple[str | None, float]],
) -> Choice:
    """A stateless pick for a clip that belongs to no take (Canary detecting
    the source of another engine's line to translate it)."""
    if len(candidates) == 1:
        return Choice(candidates[0], candidates, duration, "pinned")
    detected, share = detect()
    return LanguageTracker()._decide(candidates, duration, detected, share)


# Trackers keyed by chat session id and scoped to one take (one press of
# Record, named by the client's ``take`` id). A watchdog reconnect or a live
# engine switch repeats the take id and keeps the settled language; a new
# take gets a fresh tracker even when the last take's stop never reached the
# server, and a dropped take's tail flush keeps the tracker it started with,
# so it can never settle the next take. An explicit stop forgets the entry.
_trackers: dict[str, tuple[str, LanguageTracker]] = {}
_trackers_lock = threading.Lock()


def tracker_for(session_id: str | None, take: str = "") -> LanguageTracker:
    """The tracker of ``session_id``'s current take, created on first use and
    replaced when a different take opens. A connection without a session id
    gets a private one that nothing else shares."""
    if not session_id:
        return LanguageTracker()
    with _trackers_lock:
        entry = _trackers.get(session_id)
        if entry is None or entry[0] != take:
            entry = _trackers[session_id] = (take, LanguageTracker())
        return entry[1]


def forget_tracker(session_id: str | None, take: str = "") -> None:
    """Drop a session's tracker when ``take`` ends with an explicit stop. A
    late stop of an earlier take leaves the next take's tracker alone. No-op
    without an id."""
    if not session_id:
        return
    with _trackers_lock:
        entry = _trackers.get(session_id)
        if entry is not None and entry[0] == take:
            del _trackers[session_id]
