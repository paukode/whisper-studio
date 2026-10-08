"""One transcription connection's start-up and lost drafts, in the backend log.

Reports like "the first sentence showed and then vanished" or "the first
seconds of a recording are lost" need the order of events at the start of a
take and which side was slow: the client (mic not ready, audio buffered), the
server (model paged back in, speaker model loading), or the VAD (an utterance
dropped). Every /ws connection gets one timeline, tagged ``[rec <take>]``
(``[dictation]`` for the chat mic), so ``grep '\\[rec '`` pulls a recording
out of backend.log. It logs:

- the connection, numbered within its take (a reconnect is connection 2+);
- the client's start-up marks (Record pressed, socket open, mic ready, first
  frame, first audio sent), which the client sends as ``client_mark``;
- the first audio frame, and how much audio it carried: more than a chunk
  means the client buffered while the socket was not open yet;
- the level of each of the first seconds of audio ("silent" is digital
  zero, the sign of a capture that has not started producing sound);
- when the ASR session was ready and how long its model load took;
- for the first minute: each utterance's first draft, every final, slow
  decodes and gaps in the audio, each with how far the server runs behind
  the live audio;
- at any time: a draft withdrawn without a final, with its words and the
  backend's reason; and a summary when the connection ends.

Words go to the log only for the first minute and for withdrawn drafts.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

import numpy as np

log = logging.getLogger("whisper-studio")

SAMPLE_RATE = 16000
# Drafts and finals are logged one by one for this long after the connection
# opens (and the first finals always are), then only counted.
START_WINDOW_S = 60.0
ALWAYS_LOGGED_FINALS = 3
# Seconds of audio whose level is logged, one value per second.
LEVEL_SECONDS = 10
# A process() call this slow is a stall worth naming (a model paged back in).
SLOW_DECODE_S = 1.0
# More audio than this in one frame means the client sent a backlog.
BURST_S = 1.5
# No audio for this long inside the start window is a gap worth naming.
AUDIO_GAP_S = 1.5
TEXT_CHARS = 80
# Client mark fields copied into the log (everything else is ignored).
_MARK_FIELDS = ("seconds", "dbfs", "device", "source", "error")

# Connections per take, so a reconnect reads as "connection 2 of this take".
_connections: OrderedDict[str, int] = OrderedDict()
_connections_lock = threading.Lock()
_CONNECTIONS_KEPT = 256


def _count_connection(take_id: str) -> int:
    with _connections_lock:
        count = _connections.pop(take_id, 0) + 1
        _connections[take_id] = count
        while len(_connections) > _CONNECTIONS_KEPT:
            _connections.popitem(last=False)
    return count


def _quote(text: object) -> str:
    """Log-safe excerpt of user text: one line, bounded, quoted."""
    flat = " ".join(str(text).split())
    if len(flat) > TEXT_CHARS:
        flat = flat[: TEXT_CHARS - 3] + "..."
    return f'"{flat}"'


def _dbfs(mean_square: float) -> str:
    if mean_square <= 0.0:
        return "silent"
    return f"{10 * math.log10(mean_square):.0f}"


class RecordingTimeline:
    """The log lines of one /ws connection. Not thread-safe: the handler
    calls it from the event loop only."""

    def __init__(
        self,
        *,
        take_id: str,
        session_id: str | None,
        backend: str,
        dictation: bool,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._t0 = clock()
        take_id = take_id.strip()
        self.tag = "[dictation]" if dictation else f"[rec {take_id[:8] or 'no-take'}]"
        connection = _count_connection(take_id) if take_id and not dictation else 1
        self._audio_bytes = 0
        self._first_audio_at: float | None = None
        self._last_audio_at: float | None = None
        self._levels: list[str] = []
        self._level_sum = 0.0
        self._level_samples = 0
        self._levels_logged = False
        self._draft_open = False
        self._drafts = 0
        self._finals = 0
        self._withdrawn = 0
        self._first_final_at: float | None = None
        self._slow_decodes = 0
        self._slowest_decode = 0.0
        log.info(
            "%s connected: backend=%s, session=%s, connection %d of this take%s",
            self.tag,
            backend,
            (session_id or "none")[:8],
            connection,
            " (a reconnect)" if connection > 1 else "",
        )

    # ── clock helpers ──

    def _elapsed(self) -> float:
        return self._clock() - self._t0

    def _at(self) -> str:
        return f"+{self._elapsed():.2f}s"

    def _in_start(self) -> bool:
        return self._elapsed() <= START_WINDOW_S

    def _audio_s(self) -> float:
        return self._audio_bytes / 2 / SAMPLE_RATE

    def _lag(self) -> str:
        """How much audio has arrived, and how far the server is behind it.
        The client streams in real time, so wall time since the first frame
        minus the audio received is time the server has not read yet."""
        if self._first_audio_at is None:
            return "no audio yet"
        behind = max(0.0, (self._clock() - self._first_audio_at) - self._audio_s())
        return f"{self._audio_s():.1f} s of audio in, {behind:.1f} s behind live"

    # ── events ──

    def client_mark(self, msg: dict) -> None:
        """A start-up mark the client measured from the press of Record."""
        name = " ".join(str(msg.get("mark") or "?").split())[:40]
        ms = msg.get("ms")
        ms_text = f"{int(ms)} ms" if isinstance(ms, (int, float)) else "? ms"
        extras = [
            f"{key}={_quote(msg[key]) if isinstance(msg[key], str) else msg[key]}"
            for key in _MARK_FIELDS
            if key in msg and isinstance(msg[key], (str, int, float))
        ]
        log.info(
            "%s client: %s at %s after Record%s (server %s)",
            self.tag,
            name,
            ms_text,
            f" ({', '.join(extras)})" if extras else "",
            self._at(),
        )

    def audio(self, pcm: bytes) -> None:
        """One binary audio frame read from the socket."""
        now = self._clock()
        seconds = len(pcm) / 2 / SAMPLE_RATE
        if self._first_audio_at is None:
            self._first_audio_at = now
            log.info(
                "%s first audio at %s: %.2f s of audio in the first frame%s",
                self.tag,
                self._at(),
                seconds,
                " (the client buffered it)" if seconds > BURST_S else "",
            )
        elif self._in_start():
            if self._last_audio_at is not None and now - self._last_audio_at > AUDIO_GAP_S:
                log.info(
                    "%s no audio read for %.1f s, until %s",
                    self.tag,
                    now - self._last_audio_at,
                    self._at(),
                )
            if seconds > BURST_S:
                log.info(
                    "%s burst of %.1f s of audio in one frame at %s", self.tag, seconds, self._at()
                )
        self._last_audio_at = now
        self._audio_bytes += len(pcm)
        if not self._levels_logged:
            self._measure(pcm)

    def _measure(self, pcm: bytes) -> None:
        """Mean power of each second of the first LEVEL_SECONDS of audio."""
        usable = len(pcm) - len(pcm) % 2
        samples = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float64) / 32768.0
        i = 0
        while i < len(samples) and len(self._levels) < LEVEL_SECONDS:
            part = samples[i : i + SAMPLE_RATE - self._level_samples]
            self._level_sum += float(np.dot(part, part))
            self._level_samples += len(part)
            i += len(part)
            if self._level_samples == SAMPLE_RATE:
                self._levels.append(_dbfs(self._level_sum / SAMPLE_RATE))
                self._level_sum = 0.0
                self._level_samples = 0
        if len(self._levels) >= LEVEL_SECONDS:
            self._log_levels()

    def _log_levels(self) -> None:
        if self._levels_logged or not self._levels:
            return
        self._levels_logged = True
        log.info(
            "%s level of the first %d s of audio (dBFS per second): %s",
            self.tag,
            len(self._levels),
            " ".join(self._levels),
        )

    def session_ready(self, load_s: float | None, build_s: float) -> None:
        """The ASR session exists (built on the first audio frame)."""
        log.info(
            "%s ASR session ready at %s (%s, session build %.1f s)",
            self.tag,
            self._at(),
            "model already in memory" if load_s is None else f"model load {load_s:.1f} s",
            build_s,
        )

    def decoded(self, seconds: float) -> None:
        """One process() call on the backend's executor took ``seconds``."""
        self._slowest_decode = max(self._slowest_decode, seconds)
        if seconds < SLOW_DECODE_S:
            return
        self._slow_decodes += 1
        if self._in_start():
            log.info(
                "%s decoding one chunk took %.1f s, at %s (%s)",
                self.tag,
                seconds,
                self._at(),
                self._lag(),
            )

    def draft(self, text: str) -> None:
        """A live draft was sent. Only an utterance's first draft is logged."""
        if self._draft_open:
            return
        self._draft_open = True
        self._drafts += 1
        if self._in_start():
            log.info(
                "%s draft shown at %s (%s): %s", self.tag, self._at(), self._lag(), _quote(text)
            )

    def final(self, text: str, chunk_id: int, speaker: str, label_s: float) -> None:
        """A final was sent to the client (it replaces any open draft)."""
        self._draft_open = False
        self._finals += 1
        if self._first_final_at is None:
            self._first_final_at = self._elapsed()
        if self._in_start() or self._finals <= ALWAYS_LOGGED_FINALS:
            log.info(
                "%s final #%d at %s (%s; %s, speaker label took %.1f s): %s",
                self.tag,
                chunk_id,
                self._at(),
                self._lag(),
                speaker,
                label_s,
                _quote(text),
            )

    def withdrawn(self, why: dict | None) -> None:
        """A draft left the screen without a final: its words are lost."""
        self._draft_open = False
        self._withdrawn += 1
        why = why or {}
        details = []
        if "voiced_ms" in why:
            details.append(
                f"{why['voiced_ms']} ms voiced of {why.get('length_ms', '?')} ms, at {why.get('at', '?')}"
            )
        if why.get("decoded"):
            details.append("final decode gave " + ", ".join(_quote(t) for t in why["decoded"]))
        if why.get("error"):
            details.append(f"error {_quote(why['error'])}")
        log.warning(
            "%s DRAFT DISCARDED, not added to the transcript, at %s (%s): %s; reason: %s%s",
            self.tag,
            self._at(),
            self._lag(),
            _quote(why.get("draft", "")),
            why.get("reason", "unknown"),
            f" ({'; '.join(details)})" if details else "",
        )

    def carryover_lost(self, error: BaseException, waited_s: float) -> None:
        """A reconnect could not get the sentence the dropped connection had
        in flight."""
        kind = type(error).__name__
        log.warning(
            "%s lost the sentence a dropped connection left in flight: %s after %.1f s",
            self.tag,
            f"{kind}: {error}" if str(error) else kind,
            waited_s,
        )

    def closed(self, by_stop: bool) -> None:
        self._log_levels()
        first_final = "none" if self._first_final_at is None else f"+{self._first_final_at:.2f}s"
        log.info(
            "%s closed after %.1f s (%s): %.1f s of audio, %d drafts, %d finals, "
            "%d drafts discarded, first final at %s, slowest decode %.1f s (%d over %.0f s)",
            self.tag,
            self._elapsed(),
            "stop" if by_stop else "dropped without stop",
            self._audio_s(),
            self._drafts,
            self._finals,
            self._withdrawn,
            first_final,
            self._slowest_decode,
            self._slow_decodes,
            SLOW_DECODE_S,
        )
