#!/usr/bin/env python3
"""Measure turn splitting against real speech, not synthetic embeddings.

Why this exists: the unit tests in tests/test_speaker_turns.py stub the
encoder, so they verify the ALGORITHM but say nothing about the
THRESHOLDS. A build once shipped with a threshold calibrated on
stationary TTS single sentences, and on continuous speech it split one
person into eleven "speakers", fragmented sentences into thirds, and sent
every fragment through language ID separately. No stubbed test could have
caught that. This can.

It builds its own fixtures with macOS `say`, so it needs no corpus and no
network: several voices, each reading multiple sentences, then
concatenated into single-speaker and multi-speaker "utterances". The
single-speaker cases are the important half — those are the ones that
must NOT split.

    PYTHONPATH=. python scripts/calibrate_turn_splitting.py

Exits non-zero on any false split, or on a miss in a case not marked as
known-hard, so it can gate a release. TTS voices
are more stationary than real people, so treat a zero-margin pass as a
failure: the report prints the headroom between the largest
within-speaker distance and the bar, and that gap is the safety margin
against a real voice in a real room.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.diarization import embedder, turns  # noqa: E402

# Voice, sentences. Deliberately varied in prosody and length: a single
# short sentence per voice is the easy case and hides the failure mode.
VOICES = {
    "sam": (
        "Samantha",
        [
            "So the quarterly numbers came in much stronger than anyone expected.",
            "I think we should revisit the roadmap before the end of the month.",
            "There is a lot riding on this one decision, honestly.",
        ],
    ),
    "gpa": (
        "Grandpa (English (US))",
        [
            "Well that depends entirely on what the finance team decides to do.",
            "Right, but the hiring plan was already approved last quarter.",
            "I do not see how we get around that now.",
        ],
    ),
    "flo": (
        "Flo (English (US))",
        [
            "Can I jump in here for a second, because I have a different view.",
            "We tried that approach twice already and it did not work either time.",
            "Let us look at what actually changed since then.",
        ],
    ),
}

# name, voice keys (one per turn), expected spans, whether a miss is
# acceptable. Only false splits are treated as failures by default: they
# fragment a sentence and invent a speaker, while a miss just leaves one
# label on a mixed utterance. Two voices close enough to sit inside one
# speaker's normal variation are a documented limitation, not a bug.
CASES = [
    ("one speaker, three sentences (sam)", ["sam"], 1, False),
    ("one speaker, three sentences (gpa)", ["gpa"], 1, False),
    ("one speaker, three sentences (flo)", ["flo"], 1, False),
    ("one speaker, twice over (sam)", ["sam", "sam"], 1, False),
    ("one speaker, twice over (gpa)", ["gpa", "gpa"], 1, False),
    ("one speaker, twice over (flo)", ["flo", "flo"], 1, False),
    ("two speakers (sam -> gpa)", ["sam", "gpa"], 2, False),
    ("two speakers (gpa -> sam)", ["gpa", "sam"], 2, False),
    ("two speakers (sam -> flo)", ["sam", "flo"], 2, False),
    ("two similar speakers (gpa -> flo)", ["gpa", "flo"], 2, True),
    ("three turns (sam -> gpa -> sam)", ["sam", "gpa", "sam"], 3, False),
]


def synthesize(out_dir: str) -> dict[str, np.ndarray]:
    """One 16 kHz mono clip per voice via `say`, decoded to float32."""
    clips: dict[str, np.ndarray] = {}
    for key, (voice, sentences) in VOICES.items():
        aiff = os.path.join(out_dir, f"{key}.aiff")
        wav = os.path.join(out_dir, f"{key}.wav")
        subprocess.run(
            ["say", "-v", voice, "-o", aiff, " ".join(sentences)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", aiff, "-ar", "16000", "-ac", "1", wav],
            check=True,
            capture_output=True,
        )
        with wave.open(wav) as fh:
            raw = fh.readframes(fh.getnframes())
        clips[key] = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return clips


def fake_words(audio: np.ndarray) -> list[dict]:
    """Evenly spaced word times, back to back.

    Real word timings would come from the engine; even spacing with no
    gaps is the harder case for the splitter, because it has no silence
    to snap to.
    """
    duration = len(audio) / turns.SAMPLE_RATE
    count = max(2, int(duration / 0.4))
    step = duration / count
    return [
        {"text": f"w{i}", "start": round(i * step, 3), "end": round((i + 1) * step, 3)}
        for i in range(count)
    ]


def headroom(audio: np.ndarray) -> tuple[float, float]:
    """(largest cut distance in this audio, the bar it was judged against)."""
    windows, _centres = turns._windows(audio)
    embeddings = [e for e in embedder.encode_batch(windows) if e is not None]
    if len(embeddings) < 2:
        return (0.0, 0.0)
    matrix = np.stack(embeddings)
    distances = np.array(
        [
            d
            for d in (turns._cut_distance(matrix, k) for k in range(1, len(matrix)))
            if d is not None
        ]
    )
    if not len(distances):
        return (0.0, 0.0)
    median = float(np.median(distances))
    lower = distances[distances <= median]
    baseline = float(np.median(lower))
    spread = max(float(np.median(np.abs(lower - baseline))), turns.MIN_SPREAD)
    return float(distances.max()), max(turns.CHANGE_DISTANCE, baseline + turns.PROMINENCE * spread)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        print("synthesizing fixtures with `say` ...")
        clips = synthesize(tmp)
        print(f"encoder: {embedder.configured_name()}\n")
        print(f"  {'case':40} {'spans':>7} {'want':>5} {'peak':>6} {'bar':>6} {'margin':>7}")
        false_splits = missed = tolerated = 0
        for name, keys, want, tolerate_miss in CASES:
            audio = np.concatenate([clips[k] for k in keys])
            spans = turns.split_utterance(audio, fake_words(audio))
            peak, bar = headroom(audio)
            # For a single-speaker case the margin is how far the worst
            # within-speaker wobble stayed BELOW the bar. For a handover
            # it is how far the boundary cleared it.
            margin = (bar - peak) if want == 1 else (peak - bar)
            verdict = "ok"
            if len(spans) > want:
                verdict, false_splits = "FALSE SPLIT", false_splits + 1
            elif len(spans) < want:
                if tolerate_miss:
                    verdict, tolerated = "missed (known hard)", tolerated + 1
                else:
                    verdict, missed = "missed", missed + 1
            print(
                f"  {name:40} {len(spans):>7} {want:>5} {peak:6.3f} {bar:6.3f} "
                f"{margin:+7.3f}  {verdict}"
            )
        print(
            f"\n  false splits: {false_splits}    missed: {missed}"
            f"    missed but tolerated: {tolerated}"
        )
        if false_splits:
            print("\n  A false split fragments a sentence and invents a speaker. Raise")
            print("  CHANGE_DISTANCE or PROMINENCE in server/diarization/turns.py.")
        return 1 if (false_splits or missed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
