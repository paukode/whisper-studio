"""Split one VAD utterance at speaker turn boundaries.

The VAD buffer flushes on silence, not on speaker change, so a turn
handover with little or no gap ("...that's my point." / "Right, but—")
lands inside a single utterance and used to get a single speaker label.
Every word after the handover was then attributed to whoever spoke first.

This module finds the handover point inside the utterance and returns one
span per speaker, so the rest of the pipeline (speaker assignment,
re-clustering, the transcript UI) keeps working exactly as before and
simply sees two chunks where it used to see one.

How the boundary is found:

1. Slide a 1.25 s window (0.25 s hop) over the utterance and embed every
   window in one batched forward pass.
2. For each candidate cut, compare a bounded context of windows on its
   left against the same on its right. A real turn change makes those two
   far apart AND each side internally coherent — the second half of that
   test is what rejects a single noisy window masquerading as a speaker
   change. The context is local rather than the whole utterance because a
   global left-vs-right split is blind to a change that is not the only
   one: in A -> B -> A every global cut has A on both sides.
3. Keep the peaks, strongest first, each suppressing anything within one
   minimum turn, so one handover cannot register as three adjacent cuts.
4. Snap each surviving time to a word boundary, preferring the largest
   inter-word silence nearby, because a handover almost always has a
   small gap even when it sounds continuous.

Word timestamps are required: without them the text cannot be cut at the
right place, and a wrong text cut is worse than a wrong label. Backends
that do not report word times simply never split (see server/asr).

The boundary word or two genuinely overlaps in real conversation. That
region gets ``overlap=True`` rather than a pretend-precise cut, so the UI
can mark it and nobody reads the timing as exact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from server.diarization import embedder

log = logging.getLogger("whisper-studio")

SAMPLE_RATE = 16000

# Analysis window. 1.25 s is the shortest slice that still yields a usable
# speaker embedding (the encoder floor is 1.0 s) — shorter windows locate
# the boundary more precisely but stop carrying identity.
WINDOW_SEC = 1.25
HOP_SEC = 0.25
# Never split an utterance shorter than this: two real turns cannot both
# clear MIN_TURN_SEC inside it.
MIN_SPLIT_SEC = 2.0
# Each resulting span must be at least this long. Below ~0.8 s the span's
# own embedding is too weak to place it against the session's clusters,
# so splitting there trades one wrong label for two.
MIN_TURN_SEC = 0.8
# ...and two cuts closer together than one analysis window cannot be
# resolved independently: every window in between straddles both, so a
# single handover throws two adjacent peaks and the strip between them
# becomes a phantom third turn whose embedding is a blend of both voices.
# That blend then bridges the two real speakers into one cluster, which is
# a worse outcome than not splitting at all.
MIN_SEPARATION_SEC = WINDOW_SEC
# Absolute floor on the cosine distance that can count as a speaker
# change, whatever the utterance's own statistics say. Measured on
# ReDimNet2 with 1.25 s windows across eight single-speaker recordings and
# five two-speaker ones: the largest distance WITHIN one speaker was 0.467
# (a voice reading three sentences with varying prosody) and the smallest
# at a genuine handover was 0.608 (two deliberately similar voices). The
# floor sits in that gap, biased toward the single-speaker side because a
# false split costs far more than a missed one. scripts/
# calibrate_turn_splitting.py re-measures this on demand; keep the
# single-speaker margin comfortable, since real voices in real rooms vary
# more than the synthesized ones the harness uses.
CHANGE_DISTANCE = 0.60
# How many median absolute deviations above the utterance's own median a
# cut must stand to count as a handover rather than ordinary variation.
PROMINENCE = 4.0
# Floor on that deviation, so an unusually steady utterance (tiny MAD)
# cannot make every small wobble look prominent.
MIN_SPREAD = 0.04
# Fewer judgeable cuts than this and there is no distribution to compare
# against, so the utterance is left whole.
MIN_PROFILE_CUTS = 6
# How many windows on each side of a candidate cut are compared, and how
# many are dropped next to it because they straddle the boundary and are
# a blend of both voices. 5 windows spans ~2.5 s of audio per side.
CONTEXT_WINDOWS = 5
GUARD_WINDOWS = 1
MIN_CONTEXT_WINDOWS = 2
# How far from the acoustic boundary we look for a word gap to snap to.
SNAP_WINDOW_SEC = 0.5
# An inter-word gap at or below this means the turns abut or overlap: the
# cut is a best estimate, not a clean silence.
OVERLAP_GAP_SEC = 0.08
# Circuit breaker: after this many finalized utterances, a split rate
# above the ceiling means the detector is misreading the recording, and
# splitting turns itself off for the rest of it.
SPLIT_WARMUP = 8
SPLIT_CEILING = 0.4
# Bound on how many spans one utterance can produce. An 8 s VAD window
# holding three real handovers is vanishingly rare, and every false cut
# costs a phantom speaker, so the cap is deliberately tight.
MAX_SPANS = 3


@dataclass
class Word:
    text: str
    start: float
    end: float
    # Was this word separated from the previous one by a space in the
    # engine's output? Chinese, Japanese and Thai transcripts have no
    # spaces, and rejoining their words with one would turn "你好世界"
    # into "你 好 世 界". Defaults to True for callers that don't say.
    space: bool = True


@dataclass
class TurnSpan:
    """One speaker's stretch of an utterance."""

    text: str
    audio: np.ndarray
    start: float
    end: float
    # Mean of the window embeddings inside this span, already L2-normalized.
    # Present only for spans produced by a split: a whole unsplit utterance
    # is better embedded in one pass by the caller, with full context.
    embedding: np.ndarray | None = None
    # The cut next to this span was a hard handover with no clean silence,
    # so its boundary words may belong to either speaker.
    overlap: bool = False
    words: list[Word] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


class SplitBudget:
    """Stops turn splitting from running away on a recording it misreads.

    Genuine pauseless handovers are a minority of utterances even in a
    lively meeting, so a session where most utterances "contain a
    handover" is not a busy meeting: it is the detector misfiring on one
    person, and every misfire fragments a sentence and invents a speaker.

    After a warm-up, if the split rate exceeds the ceiling the budget
    latches closed for the rest of the recording. Latched rather than
    per-utterance so behaviour cannot flap mid-conversation, and one-way
    because the cheap failure is to stop splitting.
    """

    def __init__(self, warmup: int = SPLIT_WARMUP, ceiling: float = SPLIT_CEILING) -> None:
        self._warmup = warmup
        self._ceiling = ceiling
        self._seen = 0
        self._split = 0
        self._open = True

    @property
    def open(self) -> bool:
        return self._open

    def record(self, was_split: bool) -> None:
        """Account for one finalized utterance."""
        self._seen += 1
        if was_split:
            self._split += 1
        if self._open and self._seen >= self._warmup and self._split / self._seen > self._ceiling:
            self._open = False
            log.warning(
                "Turn splitting disabled for this recording: %d of %d utterances split, "
                "which is a misread rather than a conversation",
                self._split,
                self._seen,
            )


def normalize_words(raw: list[dict] | None) -> list[Word]:
    """Backend word dicts -> ordered Words, dropping anything malformed."""
    words: list[Word] = []
    for item in raw or []:
        try:
            text = str(item.get("text", "")).strip()
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if not text or end <= start:
            continue
        words.append(Word(text=text, start=start, end=end, space=bool(item.get("space", True))))
    words.sort(key=lambda w: w.start)
    return words


def join_words(words: list[Word]) -> str:
    """Words back into a line of transcript, in the script's own convention."""
    return "".join(
        (" " if word.space and i else "") + word.text for i, word in enumerate(words)
    ).strip()


def _windows(audio: np.ndarray) -> tuple[list[np.ndarray], np.ndarray]:
    """Sliding windows plus their centre times."""
    width = int(WINDOW_SEC * SAMPLE_RATE)
    hop = int(HOP_SEC * SAMPLE_RATE)
    slices: list[np.ndarray] = []
    centres: list[float] = []
    start = 0
    while start + width <= len(audio):
        slices.append(audio[start : start + width])
        centres.append((start + width / 2) / SAMPLE_RATE)
        start += hop
    return slices, np.asarray(centres, dtype=np.float64)


def _cut_distance(matrix: np.ndarray, k: int) -> float | None:
    """Cosine distance between the voices either side of window boundary k.

    Compares a bounded context on each side rather than the whole
    utterance, because a global left-vs-right split cannot see a change
    that is not the only one: in an A -> B -> A utterance every global cut
    has A on both sides and scores near zero. A local comparison peaks at
    each real handover independently.

    The window straddling the boundary is a blend of both voices and only
    blunts the contrast, so a one-window guard band is dropped from each
    side. Returns None when the context is too thin to judge.
    """
    left = matrix[max(0, k - GUARD_WINDOWS - CONTEXT_WINDOWS) : max(0, k - GUARD_WINDOWS)]
    right = matrix[k + GUARD_WINDOWS : k + GUARD_WINDOWS + CONTEXT_WINDOWS]
    if len(left) < MIN_CONTEXT_WINDOWS or len(right) < MIN_CONTEXT_WINDOWS:
        return None
    lc, rc = left.mean(axis=0), right.mean(axis=0)
    ln, rn = float(np.linalg.norm(lc)), float(np.linalg.norm(rc))
    if ln == 0.0 or rn == 0.0:
        return None
    return 1.0 - float((lc / ln) @ (rc / rn))


def _boundaries(matrix: np.ndarray, centres: np.ndarray, duration: float) -> list[float]:
    """Every speaker change in the utterance, strongest first then ordered.

    A speaker change is judged against THIS utterance's own variation, not
    against a fixed number. One person talking continuously already moves
    around in embedding space — pitch, energy and phonetic content all
    shift across a 1.25 s window — and measured on real speech those
    within-speaker distances run 0.13-0.38 and differ per voice. Any
    absolute threshold low enough to catch a genuine handover therefore
    sits inside one speaker's normal spread, which is what produced eleven
    "speakers" and sentences chopped into thirds.

    So the bar is the utterance's own quiet-half baseline plus a multiple
    of that half's dispersion, floored by CHANGE_DISTANCE. A real handover is an
    outlier against that background; ordinary variation, however wide, is
    not. This also self-calibrates per recording, which no constant can.

    The asymmetry is deliberate: a missed split leaves one label on a
    mixed utterance, exactly as before this module existed, while a false
    split fragments a sentence, spawns a phantom speaker, and sends every
    fragment through language ID and translation separately.
    """
    raw: list[tuple[int, float, float]] = []
    for k in range(1, len(matrix)):
        distance = _cut_distance(matrix, k)
        if distance is None:
            continue
        raw.append((k, float((centres[k - 1] + centres[k]) / 2.0), distance))
    if len(raw) < MIN_PROFILE_CUTS:
        # Too little of the utterance is judgeable to know what normal
        # variation looks like here, so decline rather than guess.
        return []
    distances = np.array([d for _k, _cut, d in raw])
    # Estimate normal variation from the LOWER HALF of the distribution.
    # The plain median is contaminated by the very thing being measured:
    # the analysis context is ~2.5 s wide, so around a genuine handover a
    # large share of cuts have mixed audio on one side and sit high. In an
    # A -> B -> A utterance that can be most of them, inflating the
    # baseline until no real boundary could ever clear the bar. The lower
    # half is the part that is definitely within-speaker.
    median = float(np.median(distances))
    lower = distances[distances <= median]
    baseline = float(np.median(lower))
    spread = max(float(np.median(np.abs(lower - baseline))), MIN_SPREAD)
    bar = max(CHANGE_DISTANCE, baseline + PROMINENCE * spread)

    chosen: list[float] = []
    for _k, cut, distance in sorted(raw, key=lambda r: -r[2]):
        if len(chosen) >= MAX_SPANS - 1:
            break
        if distance < bar:
            break  # sorted descending: nothing later can qualify either
        if cut < MIN_TURN_SEC or duration - cut < MIN_TURN_SEC:
            continue
        if all(abs(cut - taken) >= MIN_SEPARATION_SEC for taken in chosen):
            chosen.append(cut)
    return sorted(chosen)


def _snap_to_word(words: list[Word], cut: float) -> tuple[int, bool] | None:
    """Word index the second span starts at, and whether the turns abut.

    Prefers the widest inter-word silence within SNAP_WINDOW_SEC of the
    acoustic boundary (a handover nearly always leaves one), and falls
    back to the word nearest the boundary when the speech is continuous.
    """
    candidates = [i for i in range(1, len(words)) if abs(words[i].start - cut) <= SNAP_WINDOW_SEC]
    if not candidates:
        candidates = [i for i in range(1, len(words))]
        if not candidates:
            return None
        index = min(candidates, key=lambda i: abs(words[i].start - cut))
    else:
        index = max(candidates, key=lambda i: words[i].start - words[i - 1].end)
    gap = words[index].start - words[index - 1].end
    return index, gap <= OVERLAP_GAP_SEC


def _slice(
    audio: np.ndarray,
    words: list[Word],
    matrix: np.ndarray,
    centres: np.ndarray,
    start: float,
    end: float,
    overlap: bool,
) -> TurnSpan:
    """Build one span, embedding it from the windows whose centre it holds."""
    member = matrix[(centres >= start) & (centres < end)]
    embedding = None
    if len(member):
        vec = member.mean(axis=0)
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            embedding = (vec / norm).astype(np.float32)
    return TurnSpan(
        text=join_words(words),
        audio=audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)],
        start=start,
        end=end,
        embedding=embedding,
        overlap=overlap,
        words=words,
    )


def split_utterance(audio: np.ndarray | None, raw_words: list[dict] | None) -> list[TurnSpan]:
    """One utterance -> one span per speaker turn (usually just one).

    Returns a single whole-utterance span (embedding None, so the caller
    embeds it the way it always did) whenever the audio is too short, the
    words are missing, or no boundary clears the tests. Callers can treat
    a one-element result as "nothing changed".
    """
    if audio is None or len(audio) == 0:
        return []
    duration = len(audio) / SAMPLE_RATE
    words = normalize_words(raw_words)
    whole = [
        TurnSpan(
            text=join_words(words),
            audio=audio,
            start=0.0,
            end=duration,
            words=words,
        )
    ]
    if duration < MIN_SPLIT_SEC or len(words) < 2:
        return whole

    slices, centres = _windows(audio)
    if len(slices) < 2:
        return whole
    embeddings = embedder.encode_batch(slices)
    keep = [i for i, e in enumerate(embeddings) if e is not None]
    if len(keep) < 2:
        return whole
    matrix = np.stack([embeddings[i] for i in keep])
    centres = centres[keep]

    cuts = _boundaries(matrix, centres, duration)
    if not cuts:
        return whole

    spans: list[TurnSpan] = []
    cursor = 0.0
    remaining = list(words)
    carried_overlap = False
    for cut_time in cuts:
        snapped = _snap_to_word(remaining, cut_time)
        if snapped is None:
            continue
        index, abuts = snapped
        head, tail = remaining[:index], remaining[index:]
        if not head or not tail:
            continue
        # Cut the audio at the real silence between the two words, not at
        # the acoustic estimate: the words are what the user reads.
        boundary = (head[-1].end + tail[0].start) / 2.0
        # Snapping to a word can pull a cut well away from where the
        # acoustics put it, so the minimum turn length is enforced here,
        # on the span that actually results, and not only on the raw cut.
        if boundary - cursor < MIN_TURN_SEC or duration - boundary < MIN_TURN_SEC:
            continue
        remaining = tail
        spans.append(
            _slice(audio, head, matrix, centres, cursor, boundary, carried_overlap or abuts)
        )
        cursor = boundary
        carried_overlap = abuts
    if remaining:
        spans.append(_slice(audio, remaining, matrix, centres, cursor, duration, carried_overlap))
    # A degenerate cut (everything landed on one side) means no real split.
    return spans if len(spans) > 1 else whole
