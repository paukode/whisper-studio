"""VAD-bounded utterance buffer.

Audio arrives as short PCM16 mono 16 kHz chunks (the pcm-processor worklet or
the native capture). The ASR models and the speaker encoder need longer,
coherent windows, so this module accumulates chunks server-side, levels them
with an automatic gain (AutoGain), and flushes a complete utterance only when
``webrtcvad`` reports a natural silence boundary (or a hard 8 s cap). Each
flushed utterance is a 0.4-8 s window.

``UtteranceBuffer.pending`` exposes the utterance still being collected, so a
streaming backend can decode a growing window before the boundary arrives.
"""

from __future__ import annotations

import logging

import numpy as np
import webrtcvad

log = logging.getLogger("whisper-studio")

# ── Audio format constants (must match the pcm-processor worklet and the
# native capture format: PCM16 mono 16 kHz) ──
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
# 8 s is still a coherent window for Whisper and far longer than the speaker
# encoder's ~3 s, so neither hallucination nor diarization quality regresses. Most
# speech has a 400 ms+ pause well before this, so the cap rarely fires —
# it's a latency ceiling, not the common path.
MAX_UTTERANCE_MS = 8000
# How much continuous silence after speech marks the end of an utterance.
# 400 ms is short enough to feel responsive but long enough to ride
# through normal mid-sentence pauses (around 200-300 ms in English).
TRAILING_SILENCE_MS = 400

# ── Automatic gain ──
# Quiet capture is the norm, not the exception: a soft talker a metre from
# a laptop mic sits around -50 dBFS, and a system-audio tap's amplitude
# scales with the OUTPUT volume, so a call captured at 30% volume arrives
# at a third of the level the same call has at full volume. Both land
# below the VAD's sensitivity and the backends' energy gates, and the
# words silently vanish. The browser's autoGainControl only covers the
# microphone; nothing covered the tap. Normalizing here, in front of the
# VAD, fixes every source at once.
#
# Speech level the gain steers toward (~-24 dBFS, comfortable for the
# VAD, the recognizers, and the speaker encoder alike).
AGC_TARGET_RMS = 0.06
# Never amplify beyond this (+21.6 dB): past it, the floor of any real
# room comes up far enough to read as speech.
AGC_MAX_GAIN = 12.0
# Frames quieter than this (~-58 dBFS) are ambient, not speech, and must
# not steer the gain — boosting room tone until it trips the VAD is how
# an AGC manufactures phantom utterances. They still get the CURRENT gain
# applied, so levels stay continuous across a pause.
AGC_NOISE_FLOOR = 0.0012
# Per-frame smoothing toward a HIGHER gain (~0.5 s to converge at 30 ms
# frames). Drops toward a lower gain are immediate: ramping down slowly
# would clip the first loud syllable after a quiet stretch.
AGC_RISE = 0.06


class AutoGain:
    """Frame-by-frame upward-only gain rider for PCM16 speech.

    Upward-only by design: attenuating loud audio would fight the
    browser's own microphone AGC, and int16 input is already bounded.
    All it does is lift quiet capture to a workable level, smoothly,
    with saturation instead of wraparound when the rare hot frame
    arrives mid-ramp.
    """

    def __init__(self) -> None:
        self._gain = 1.0

    @property
    def gain(self) -> float:
        return self._gain

    def process(self, frame: bytes) -> bytes:
        """One PCM16 frame in, the same frame at the ridden gain out."""
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
        if rms > AGC_NOISE_FLOOR:
            desired = min(max(AGC_TARGET_RMS / rms, 1.0), AGC_MAX_GAIN)
            if desired < self._gain:
                self._gain = desired
            else:
                self._gain += AGC_RISE * (desired - self._gain)
        if self._gain <= 1.0:
            return frame
        boosted = np.clip(samples * self._gain, -1.0, 0.999969)
        return (boosted * 32768.0).astype(np.int16).tobytes()


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
        # Level-normalize every frame BEFORE the VAD sees it — see the AGC
        # block above for why quiet capture otherwise never becomes text.
        self._agc = AutoGain()
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
        # The last utterance dropped for too little voiced audio, until a
        # backend takes it (see take_discard).
        self._discard: dict | None = None

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add raw PCM16 bytes; return any utterances completed by this chunk."""
        self._partial.extend(chunk)
        flushed: list[bytes] = []

        while len(self._partial) >= FRAME_BYTES:
            frame = self._agc.process(bytes(self._partial[:FRAME_BYTES]))
            del self._partial[:FRAME_BYTES]

            is_speech = self._vad.is_speech(frame, SAMPLE_RATE)

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
                    else:
                        self._note_discard("pause")
                    self._reset_utterance()
            # else: dead silence between utterances — drop the frame so
            # we don't waste downstream model time on it.

            # Hard cap.
            if self._utterance_ms() >= MAX_UTTERANCE_MS:
                if self._voiced_ms >= MIN_UTTERANCE_MS:
                    flushed.append(bytes(self._utterance))
                else:
                    self._note_discard("length cap")
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
        elif self._in_speech:
            self._note_discard("stop")
        self._reset_utterance()
        self._partial.clear()
        return data

    def take_discard(self) -> dict | None:
        """The last utterance dropped for too little voiced audio since the
        previous call, then forget it: ``{"voiced_ms", "length_ms", "at"}``
        where ``at`` is "pause", "length cap" or "stop". A backend reads it to
        say why a draft on screen ended without a final."""
        discard, self._discard = self._discard, None
        return discard

    # ── internals ──
    def _note_discard(self, at: str) -> None:
        self._discard = {"voiced_ms": self._voiced_ms, "length_ms": self._utterance_ms(), "at": at}

    def _reset_utterance(self) -> None:
        self._utterance = bytearray()
        self._voiced_ms = 0
        self._silence_ms = 0
        self._in_speech = False

    def _utterance_ms(self) -> int:
        return (len(self._utterance) // BYTES_PER_SAMPLE) * 1000 // SAMPLE_RATE
