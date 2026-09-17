"""The AutoGain rider in front of the VAD.

Shipped after quiet capture silently produced no transcript at all: a
soft talker sits around -50 dBFS and a system-audio tap scales with the
OUTPUT volume, both below what the VAD and the backends' energy gates
accept. These tests pin the rider's contract: lift quiet speech, leave
loud speech alone, never let ambient noise steer the gain, never wrap.
"""

import numpy as np

from server.audio_buffer import AGC_MAX_GAIN, AGC_NOISE_FLOOR, AGC_TARGET_RMS, AutoGain

SR = 16000
FRAME = 480  # 30 ms @ 16 kHz, same as the VAD frames


def _frame(amplitude: float, freq: float = 220.0, phase: int = 0) -> bytes:
    t = (np.arange(FRAME) + phase * FRAME) / SR
    x = np.sin(2 * np.pi * freq * t) * amplitude
    return (x * 32767).astype(np.int16).tobytes()


def _rms(frame: bytes) -> float:
    x = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def test_quiet_speech_is_lifted_toward_the_target():
    agc = AutoGain()
    out = b""
    for i in range(60):  # ~1.8 s of steady quiet input
        out = agc.process(_frame(0.01, phase=i))
    assert _rms(out) > 5 * _rms(_frame(0.01))
    assert abs(_rms(out) - AGC_TARGET_RMS) < 0.02


def test_loud_speech_passes_through_unchanged():
    agc = AutoGain()
    frame = _frame(0.30)
    assert agc.process(frame) == frame
    assert agc.gain == 1.0


def test_ambient_noise_never_steers_the_gain():
    """Room tone must not be amplified until it trips the VAD."""
    agc = AutoGain()
    quiet_noise = _frame(AGC_NOISE_FLOOR * 0.5)
    for _ in range(200):
        agc.process(quiet_noise)
    assert agc.gain == 1.0


def test_gain_is_capped():
    agc = AutoGain()
    for i in range(400):
        agc.process(_frame(0.001, phase=i))
    assert agc.gain <= AGC_MAX_GAIN + 1e-6


def test_gain_drops_immediately_when_the_room_gets_loud():
    """A slow ramp-down would clip the first loud syllable."""
    agc = AutoGain()
    for i in range(60):
        agc.process(_frame(0.01, phase=i))
    ramped = agc.gain
    assert ramped > 3.0
    agc.process(_frame(0.3))
    assert agc.gain < ramped / 2


def test_hot_frames_saturate_instead_of_wrapping():
    """int16 overflow wraps to the opposite sign and sounds like a crack."""
    agc = AutoGain()
    for i in range(60):
        agc.process(_frame(0.01, phase=i))
    out = agc.process(_frame(0.9))
    x = np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0
    # A wrap would flip peaks to the other sign; saturation keeps the
    # waveform shape with flat tops.
    assert np.max(np.abs(x)) <= 1.0
    src = np.frombuffer(_frame(0.9), dtype=np.int16).astype(np.float32)
    assert np.all(np.sign(x[np.abs(src) > 8000]) == np.sign(src[np.abs(src) > 8000]))


def test_gain_rides_through_pauses_without_jumping():
    """Silent frames keep the current gain so levels stay continuous."""
    agc = AutoGain()
    for i in range(60):
        agc.process(_frame(0.01, phase=i))
    held = agc.gain
    for _ in range(30):  # ~1 s pause
        agc.process(_frame(0.0))
    assert agc.gain == held
