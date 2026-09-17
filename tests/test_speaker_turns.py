"""Turn splitting inside one VAD utterance.

The encoder is stubbed with synthetic orthogonal vectors so no model
loads: window k of the fake "speech" embeds to speaker A or speaker B
purely as a function of its position, which gives exact control over
where the handover is and whether one should be detected at all.
"""

import numpy as np

from server.diarization import turns

SR = turns.SAMPLE_RATE


def _audio(seconds: float) -> np.ndarray:
    """Non-silent audio of a given length (content is irrelevant, the encoder is stubbed)."""
    t = np.arange(int(seconds * SR), dtype=np.float32) / SR
    return (np.sin(2 * np.pi * 180 * t) * 0.2).astype(np.float32)


def _words(count: int, seconds: float, gap_at: int | None = None) -> list[dict]:
    """`count` back-to-back words, optionally with one real pause before word `gap_at`.

    Back-to-back is the hard case on purpose: continuous speech gives the
    splitter no silence to snap to, which is what marks a turn as
    overlapping. ``gap_at`` inserts the pause a polite handover would have.
    """
    step = (seconds - 0.3) / count
    out = []
    for i in range(count):
        start = i * step
        if gap_at is not None and i >= gap_at:
            start += 0.18  # a silence the splitter should prefer to cut at
        out.append({"text": f"w{i}", "start": round(start, 3), "end": round(start + step, 3)})
    return out


def _speakers(switch_at_sec: float | None):
    """Stub encoder: windows before the switch embed to A, after it to B."""

    def encode_batch(windows: list[np.ndarray]) -> list[np.ndarray | None]:
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0], dtype=np.float32)
        out = []
        for i in range(len(windows)):
            centre = (i * turns.HOP_SEC) + turns.WINDOW_SEC / 2
            out.append(a if switch_at_sec is None or centre < switch_at_sec else b)
        return out

    return encode_batch


def test_single_speaker_utterance_is_not_split(monkeypatch):
    monkeypatch.setattr(turns.embedder, "encode_batch", _speakers(None))
    spans = turns.split_utterance(_audio(6.0), _words(12, 6.0))
    assert len(spans) == 1
    assert spans[0].embedding is None  # caller embeds the whole utterance as before


def test_handover_splits_into_two_turns(monkeypatch):
    monkeypatch.setattr(turns.embedder, "encode_batch", _speakers(3.0))
    spans = turns.split_utterance(_audio(6.0), _words(12, 6.0, gap_at=6))
    assert len(spans) == 2
    first, second = spans
    assert first.end == second.start
    # The cut lands near the acoustic change, not at either extreme.
    assert 2.0 < first.end < 4.0
    # Words are partitioned, never duplicated or dropped.
    assert [w["text"] for w in _words(12, 6.0, gap_at=6)] == [
        w.text for w in first.words + second.words
    ]
    # Each side carries its own embedding, so each is placed independently.
    assert first.embedding is not None and second.embedding is not None
    assert float(first.embedding @ second.embedding) < 0.5


def test_short_utterance_is_never_split(monkeypatch):
    monkeypatch.setattr(turns.embedder, "encode_batch", _speakers(0.9))
    spans = turns.split_utterance(_audio(1.8), _words(6, 1.8))
    assert len(spans) == 1


def test_missing_word_timings_leave_the_utterance_whole(monkeypatch):
    """No word times means no safe place to cut the TEXT, so we do not cut at all."""
    monkeypatch.setattr(turns.embedder, "encode_batch", _speakers(3.0))
    assert len(turns.split_utterance(_audio(6.0), None)) == 1
    assert len(turns.split_utterance(_audio(6.0), [])) == 1


def test_abutting_turns_are_flagged_as_overlap(monkeypatch):
    """No silence at the handover: the boundary is an estimate and says so."""
    monkeypatch.setattr(turns.embedder, "encode_batch", _speakers(3.0))
    spans = turns.split_utterance(_audio(6.0), _words(12, 6.0))
    assert len(spans) == 2
    assert spans[1].overlap is True


def test_clean_pause_is_not_flagged_as_overlap(monkeypatch):
    monkeypatch.setattr(turns.embedder, "encode_batch", _speakers(3.0))
    spans = turns.split_utterance(_audio(6.0), _words(12, 6.0, gap_at=6))
    assert spans[1].overlap is False


def test_three_turns_resolve(monkeypatch):
    """A -> B -> A inside one utterance still separates."""

    def encode_batch(windows):
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0], dtype=np.float32)
        return [
            b if 3.0 <= (i * turns.HOP_SEC + turns.WINDOW_SEC / 2) < 6.0 else a
            for i in range(len(windows))
        ]

    monkeypatch.setattr(turns.embedder, "encode_batch", encode_batch)
    spans = turns.split_utterance(_audio(9.0), _words(18, 9.0))
    assert len(spans) == 3


def test_normalize_words_drops_malformed_entries():
    words = turns.normalize_words(
        [
            {"text": "second", "start": 1.0, "end": 1.4},
            {"text": "first", "start": 0.1, "end": 0.5},
            {"text": "", "start": 2.0, "end": 2.4},  # no text
            {"text": "backwards", "start": 3.0, "end": 2.0},  # ends before it starts
            {"text": "missing", "start": None, "end": 4.0},
        ]
    )
    assert [w.text for w in words] == ["first", "second"]


def test_one_transition_yields_one_cut(monkeypatch):
    """A handover is a ~1 s crossfade, not a step.

    The windows spanning it are a blend of both voices, so a naive peak
    search fires twice and the strip between the two cuts becomes a
    phantom third turn whose embedding bridges the two real speakers.
    """
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    blend = np.array([0.7, 0.7], dtype=np.float32) / np.linalg.norm([0.7, 0.7])

    def encode_batch(windows):
        out = []
        for i in range(len(windows)):
            centre = (i * turns.HOP_SEC) + turns.WINDOW_SEC / 2
            if centre < 2.6:
                out.append(a)
            elif centre < 3.4:  # the crossfade, ~1 window wide
                out.append(blend)
            else:
                out.append(b)
        return out

    monkeypatch.setattr(turns.embedder, "encode_batch", encode_batch)
    spans = turns.split_utterance(_audio(6.0), _words(12, 6.0))
    assert len(spans) == 2, [f"{s.start:.2f}-{s.end:.2f}" for s in spans]
    assert all(s.duration >= turns.MIN_TURN_SEC for s in spans)


def test_no_span_is_shorter_than_a_turn(monkeypatch):
    """Snapping to a word can drag a cut far from the acoustic estimate."""
    monkeypatch.setattr(turns.embedder, "encode_batch", _speakers(3.0))
    spans = turns.split_utterance(_audio(6.0), _words(4, 6.0))
    assert all(s.duration >= turns.MIN_TURN_SEC for s in spans)


def test_words_rejoin_in_the_script_s_own_convention():
    """Chinese has no spaces; rejoining a split turn must not invent them."""
    spaced = turns.normalize_words(
        [
            {"text": "the", "start": 0.0, "end": 0.3, "space": False},
            {"text": "quarterly", "start": 0.3, "end": 0.8, "space": True},
            {"text": "numbers", "start": 0.8, "end": 1.2, "space": True},
        ]
    )
    assert turns.join_words(spaced) == "the quarterly numbers"
    unspaced = turns.normalize_words(
        [
            {"text": "你好", "start": 0.0, "end": 0.3, "space": False},
            {"text": "世界", "start": 0.3, "end": 0.6, "space": False},
        ]
    )
    assert turns.join_words(unspaced) == "你好世界"


# ── the circuit breaker ──────────────────────────────────────────────────────
# Shipped after a build where the detector misread one person as eleven: a
# recording where most utterances "contain a handover" is a misread, and
# the cheap failure is to stop splitting.


def test_split_budget_latches_off_when_most_utterances_split():
    budget = turns.SplitBudget(warmup=8, ceiling=0.4)
    for _ in range(8):
        assert budget.open
        budget.record(True)
    assert not budget.open
    # One-way: a quiet stretch afterwards does not re-open it.
    for _ in range(20):
        budget.record(False)
    assert not budget.open


def test_split_budget_leaves_a_real_conversation_alone():
    budget = turns.SplitBudget(warmup=8, ceiling=0.4)
    # A lively meeting: roughly one utterance in four holds a handover.
    for i in range(40):
        budget.record(i % 4 == 0)
        assert budget.open


def test_split_budget_waits_out_the_warmup():
    """Two splits in the first two utterances is not yet evidence."""
    budget = turns.SplitBudget(warmup=8, ceiling=0.4)
    budget.record(True)
    budget.record(True)
    assert budget.open
