"""The cheap goal evaluator: one structured call that judges whether the goal
is met from a rendered transcript tail.

Runs on a CHEAP model (Haiku for Anthropic sessions; a Haiku fallback judges
GPT sessions too — cross-provider judging is acceptable and keeps cost low).
Output is coerced with a hand-rolled tolerant parser (no new dependency): the
first ``{...}`` block is extracted, one retry on parse failure, and an
allow-with-warning verdict is returned rather than wedging the turn when no
model is configured or the call fails.
"""

from __future__ import annotations

import json
import logging
import re

from server.goals import Verdict
from server.goals.tail import render_tail

log = logging.getLogger("whisper-studio")

_MAX_TOKENS = 400

_SYSTEM = (
    "You are a strict completion evaluator inside a coding agent's loop. "
    "You are given a GOAL and the tail of the agent's transcript. Decide whether "
    "the goal is genuinely achieved based on EVIDENCE in the transcript, not the "
    "agent's own claims. If the transcript contains a verify_change result, weight "
    "'VERIFY PASS' / 'VERIFY FAIL' above any prose the agent wrote. Be skeptical of "
    "'done!' with no supporting tool output.\n\n"
    "Reply with ONLY a JSON object, no prose, no code fence:\n"
    '{"verdict": "achieved" | "not_achieved" | "blocked", '
    '"feedback": "<one or two sentences: what is left, or why it is blocked>", '
    '"confidence": <0.0-1.0>}\n\n'
    "verdict meanings: 'achieved' = goal is genuinely met; 'not_achieved' = more "
    "work is needed (feedback says what); 'blocked' = the goal cannot be completed "
    "without something outside the agent's control (feedback says what)."
)


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced {...} object out of a model reply and parse it."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    val = json.loads(text[start : i + 1])
                    return val if isinstance(val, dict) else None
                except (ValueError, TypeError):
                    return None
    return None


def _coerce(data: dict) -> Verdict:
    """Coerce a raw dict into a valid Verdict, clamping out-of-enum values."""
    v = str(data.get("verdict", "not_achieved")).strip().lower()
    if v not in ("achieved", "not_achieved", "blocked"):
        # Tolerate near-misses ("complete", "done", "incomplete", ...).
        if v in ("done", "complete", "completed", "success"):
            v = "achieved"
        elif v in ("blocked", "stuck", "cannot"):
            v = "blocked"
        else:
            v = "not_achieved"
    try:
        conf = float(data.get("confidence", 0.0))
    except (ValueError, TypeError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return Verdict(verdict=v, feedback=str(data.get("feedback", "")).strip(), confidence=conf)


def _one_shot(system: str, user: str) -> str | None:
    """Run the cheap single completion, or None if no usable model / it fails."""
    try:
        from server.infrastructure.oneshot import one_shot

        return one_shot(system, user, max_tokens=_MAX_TOKENS, cloud_model_key="haiku")
    except Exception as e:
        log.info("Goal evaluator one_shot unavailable: %s", e)
        return None


def evaluate(goal: str, messages: list, *, provider: str = "anthropic") -> Verdict:
    """Judge whether ``goal`` is achieved from the transcript tail. Returns an
    allow-with-warning verdict (achieved, low confidence) if no evaluator model
    is available, so a missing model never wedges the turn."""
    tail = render_tail(messages)
    user = f"GOAL:\n{goal.strip()}\n\nTRANSCRIPT TAIL:\n{tail}\n\nJSON verdict:"

    raw = _one_shot(_SYSTEM, user)
    if raw is None:
        return Verdict(
            verdict="achieved",
            feedback="(goal evaluator unavailable — no cheap model configured; allowing turn to end)",
            confidence=0.0,
        )
    data = _extract_json(raw)
    if data is None:
        # One retry with an explicit nudge.
        raw = _one_shot(_SYSTEM, user + "\n\nRespond with ONLY the JSON object.")
        data = _extract_json(raw or "")
    if data is None:
        log.info("Goal evaluator returned unparseable output; allowing end.")
        return Verdict(
            verdict="achieved", feedback="(evaluator output unparseable)", confidence=0.0
        )
    return _coerce(data)


def looks_verified(messages: list) -> bool:
    """True if the transcript tail contains a passing verify_change token — a
    deterministic signal the gate can trust over the evaluator's prose."""
    tail = render_tail(messages)
    return bool(re.search(r"\bVERIFY PASS\b", tail))
