"""VAD-bounded utterance buffer.

The browser streams ~256 ms PCM16 chunks, but Whisper-large-v3-turbo and
the ECAPA speaker encoder are both trained on much longer windows (30 s
and ~3 s respectively). Feeding them 256 ms slices is the root cause of
the "Cheers cheers cheers" / "Ola ola" hallucinations and of the
diarization over-segmenting (74 IDs from 6 real speakers in 3 hours).

This module accumulates chunks server-side and flushes a complete
utterance only when ``webrtcvad`` reports a natural silence boundary
(or a hard 8 s cap). Each flushed utterance is a coherent 0.4-8 s
window — exactly what the downstream models expect.

Latency trade-off: transcripts now appear after a ~400 ms trailing
silence instead of every 256 ms. Acceptable for meeting transcription;
the alternative is unusable output.
"""

from __future__ import annotations

import logging

import webrtcvad

log = logging.getLogger("whisper-studio")

# ── Audio format constants (must match browser-side ScriptProcessor) ──
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # PCM16

# webrtcvad requires 10, 20, or 30 ms frames at 8/16/32/48 kHz. 30 ms is
# the most permissive for energy-driven decisions and the typical choice
# for telephony VAD.
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480
FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE  # 960

# ── Utterance gating ──
# Drop sub-400 ms utterances — coughs, mouse clicks, single-word
# backchannels that produce embeddings worse than no embedding at all.
MIN_UTTERANCE_MS = 400
# Hard cap so monologues don't grow unbounded in RAM and don't exceed
# Whisper's 30 s training window (which would trigger the same internal
# padding/hallucination behaviour we're trying to avoid).
#
# Set to 8 s rather than the full 30 s headroom: a pauseless monologue
# otherwise shows nothing until the cap fires, so this bounds worst-case
# latency for exactly the "I talk a lot and it arrives as one lump" case.
# 8 s is still a coherent window for Whisper and far longer than ECAPA's
# ~3 s, so neither hallucination nor diarization quality regresses. Most
# speech has a 400 ms+ pause well before this, so the cap rarely fires —
# it's a latency ceiling, not the common path.
MAX_UTTERANCE_MS = 8000
# How much continuous silence after speech marks the end of an utterance.
# 400 ms is short enough to feel responsive but long enough to ride
# through normal mid-sentence pauses (around 200-300 ms in English).
TRAILING_SILENCE_MS = 400


class UtteranceBuffer:
    """Per-connection VAD buffer.

    Usage:
        buf = UtteranceBuffer()
        for utterance_bytes in buf.feed(raw_chunk):
            # process utterance_bytes (PCM16 mono @ 16 kHz)
            ...
        # On stop:
        tail = buf.flush()
        if tail is not None:
            ...
    """

    def __init__(
        self,
        aggressiveness: int = 2,
        trailing_silence_ms: int = TRAILING_SILENCE_MS,
    ) -> None:
        # Aggressiveness 0-3: higher = more eager to mark frames as
        # non-speech. 2 is the sweet spot for desktop mic capture —
        # 3 clips faint speakers, 0-1 leaks too much room tone.
        self._vad = webrtcvad.Vad(aggressiveness)
        # Per-instance so backends can trade boundary latency against
        # mid-sentence splits (e.g. Parakeet runs slightly tighter because
        # its interims already carry the perceived latency).
        self._trailing_silence_ms = trailing_silence_ms
        # Pending bytes that haven't yet formed a complete 30 ms frame.
        self._partial = bytearray()
        # The utterance we're currently building.
        self._utterance = bytearray()
        # Voiced-frame budget for the current utterance (in ms).
        self._voiced_ms = 0
        # Trailing silence accumulated since the last voiced frame.
        self._silence_ms = 0
        # Whether the current utterance has ever contained speech.
        self._in_speech = False

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add raw PCM16 bytes; return any utterances completed by this chunk."""
        self._partial.extend(chunk)
        flushed: list[bytes] = []

        while len(self._partial) >= FRAME_BYTES:
            frame = bytes(self._partial[:FRAME_BYTES])
            del self._partial[:FRAME_BYTES]

            try:
                is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
            except Exception as exc:
                # Defensive: a malformed frame shouldn't kill the stream.
                log.debug("VAD frame error: %s", exc)
                is_speech = False

            if is_speech:
                self._utterance.extend(frame)
                self._voiced_ms += FRAME_MS
                self._silence_ms = 0
                self._in_speech = True
            elif self._in_speech:
                # Keep the silence inside the utterance — Whisper uses
                # pause structure to infer sentence boundaries.
                self._utterance.extend(frame)
                self._silence_ms += FRAME_MS

                if self._silence_ms >= self._trailing_silence_ms:
                    if self._voiced_ms >= MIN_UTTERANCE_MS:
                        flushed.append(bytes(self._utterance))
                    self._reset_utterance()
            # else: dead silence between utterances — drop the frame so
            # we don't waste downstream model time on it.

            # Hard cap.
            if self._utterance_ms() >= MAX_UTTERANCE_MS:
                if self._voiced_ms >= MIN_UTTERANCE_MS:
                    flushed.append(bytes(self._utterance))
                self._reset_utterance()

        return flushed

    def pending(self) -> bytes:
        """The in-flight utterance so far (PCM16), or ``b""`` if no speech has
        started since the last boundary.

        Read-only — it does NOT consume or alter the buffer. The streaming
        (Parakeet) backend uses this to decode a *growing* window every chunk
        for low-latency, word-by-word interim updates, while ``feed`` keeps
        owning the actual utterance gating. The Whisper path never calls it,
        so its behaviour is unchanged.
        """
        return bytes(self._utterance) if self._in_speech else b""

    def flush(self) -> bytes | None:
        """Emit any in-flight utterance (call on session stop / disconnect)."""
        data: bytes | None = None
        if self._voiced_ms >= MIN_UTTERANCE_MS:
            data = bytes(self._utterance)
        self._reset_utterance()
        self._partial.clear()
        return data

    # ── internals ──
    def _reset_utterance(self) -> None:
        self._utterance = bytearray()
        self._voiced_ms = 0
        self._silence_ms = 0
        self._in_speech = False

    def _utterance_ms(self) -> int:
        return (len(self._utterance) // BYTES_PER_SAMPLE) * 1000 // SAMPLE_RATE
