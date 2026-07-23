"""Goal loop + completion gate — "give it a goal, it achieves it".

A session can carry a goal. At each real end-of-turn, every loop (chat Claude,
chat GPT, on-device, cron, and WS-D workflows) calls ``run_completion_gate``.
The gate runs the WS-I Stop hooks first (deterministic, cheap) and then, if a
goal is active, a cheap structured evaluator that judges the transcript tail.
A ``block`` decision makes the loop inject the feedback and keep going toward
the goal, bounded by a consecutive-block cap (Claude Code parity: 8).

This package is pure decision logic — no FastAPI, no SSE — so the three loop
call sites stay tiny and WS-D can call the gate directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Consecutive-block cap (Claude Code parity). Overridable via the
# goal_max_consecutive_blocks config key.
DEFAULT_MAX_CONSECUTIVE_BLOCKS = 8

# A blocked verdict at or above this confidence ends the turn immediately
# (the goal is judged genuinely unreachable, not merely unfinished).
CONFIDENT_BLOCK_THRESHOLD = 0.7


@dataclass
class Verdict:
    """The evaluator's judgment of whether the goal is met."""

    verdict: str = "not_achieved"  # "achieved" | "not_achieved" | "blocked"
    feedback: str = ""
    confidence: float = 0.0

    @property
    def is_achieved(self) -> bool:
        return self.verdict == "achieved"

    @property
    def is_blocked(self) -> bool:
        return self.verdict == "blocked"


@dataclass
class GateContext:
    """Everything the gate needs, assembled by each caller. Provider-neutral:
    ``messages`` is the caller's native history (Anthropic blocks or Responses
    items) — the tail renderer flattens either to text."""

    session_id: str
    messages: list = field(default_factory=list)
    goal: str = ""
    provider: str = "anthropic"  # "anthropic" | "openai" | "local"
    model_id: str = ""
    workspace: str | None = None
    last_text: str = ""
    # How many times the gate has already blocked this turn (the caller owns the
    # per-turn counter; goal_state owns the cross-turn one).
    attempt: int = 0
    max_consecutive_blocks: int = DEFAULT_MAX_CONSECUTIVE_BLOCKS


@dataclass
class GateDecision:
    """The gate's answer. ``block`` True means: do not end the turn; inject
    ``feedback`` as a user message and loop again."""

    block: bool = False
    feedback: str = ""
    # Ready-to-serialize SSE payload describing why (goal_eval or stop_hook_block).
    frame: dict | None = None
    # True when a goal existed and the evaluator judged it achieved this turn.
    goal_achieved: bool = False
    source: str = ""  # "stop_hook" | "evaluator" | "cap" | ""


__all__ = [
    "CONFIDENT_BLOCK_THRESHOLD",
    "DEFAULT_MAX_CONSECUTIVE_BLOCKS",
    "GateContext",
    "GateDecision",
    "Verdict",
]
