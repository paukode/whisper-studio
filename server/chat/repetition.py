"""Repetition-loop detection for truncated (max_tokens) rounds.

A model in a degenerate repetition loop can spend its entire output budget
echoing one fragment. The engine's ``max_tokens`` continuation would then ask
it to "continue exactly where you left off", stitching more of the same text
onto the answer, round after round, until the round cap. This detects a
repetition-dominated fragment BEFORE the continuation so the turn ends with a
clear note instead. Deliberately conservative: only long verbatim repeats
(60+ chars) that cover at least half of a 400+ char fragment trip it.
"""

from __future__ import annotations

import math
from collections import Counter

MIN_FRAGMENT_LENGTH = 400
REPEAT_WINDOW = 60
MIN_REPEAT_COUNT = 5
DOMINANCE_RATIO = 0.5


def _line_repetition_dominated(text: str, n: int) -> bool:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < MIN_REPEAT_COUNT:
        return False
    counts = Counter(lines)
    line, count = counts.most_common(1)[0]
    return count >= MIN_REPEAT_COUNT and count * len(line) >= n * DOMINANCE_RATIO


def is_repetition_dominated(text: str) -> bool:
    """True when one 60+ char substring recurs often enough to cover at least
    half of ``text``. Fails open (False) for short or non-string input."""
    if not isinstance(text, str):
        return False
    n = len(text)
    if n < MIN_FRAGMENT_LENGTH:
        return False
    if _line_repetition_dominated(text, n):
        return True
    window = REPEAT_WINDOW
    needed = max(MIN_REPEAT_COUNT, math.ceil(n * DOMINANCE_RATIO / window))
    counts: dict[str, int] = {}
    for i in range(n - window + 1):
        key = text[i : i + window]
        c = counts.get(key, 0) + 1
        if c >= needed:
            return True
        counts[key] = c
    return False


def assistant_text(content) -> str:
    """Flatten an assistant round's content blocks to their text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


__all__ = [
    "DOMINANCE_RATIO",
    "MIN_FRAGMENT_LENGTH",
    "MIN_REPEAT_COUNT",
    "REPEAT_WINDOW",
    "assistant_text",
    "is_repetition_dominated",
]
