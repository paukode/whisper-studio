"""Goal loop and completion gate: "give it a goal, it achieves it".

A session can carry a goal. At each real end of an interactive chat turn the
engine calls ``run_completion_gate``: on every cloud turn, and on an on-device
turn only while a goal is active (TurnPolicy.gate_requires_goal). The gate
runs the WS-I Stop hooks and the deterministic checks first and then, if a
goal is active, a structured judge of the transcript tail: the
goal_evaluator model for a cloud session, the session's own resident model
for an on-device one. A ``block`` decision makes the loop inject the feedback
and keep going toward the goal, bounded by a consecutive-block cap (Claude
Code parity: 8). Cron verifies its runs with the same evaluator
(cron_verify.py).

This package is pure decision logic (no FastAPI, no SSE), so the loop call
site stays tiny.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# Consecutive-block cap (Claude Code parity). Overridable via the
# goal_max_consecutive_blocks config key.
DEFAULT_MAX_CONSECUTIVE_BLOCKS = 8

# A blocked verdict at or above this confidence ends the turn immediately
# (the goal is judged genuinely unreachable, not merely unfinished).
CONFIDENT_BLOCK_THRESHOLD = 0.7


@dataclass
class Verdict:
    """The evaluator's judgment of whether the goal is met.

    ``not_checked`` is not a judgment: the judge could not run, or its answer
    could not be understood. ``feedback`` then holds the reason. It ends the
    turn, keeps the goal active, is not counted as a block and is never
    recorded as achieved."""

    verdict: str = "not_achieved"  # "achieved" | "not_achieved" | "blocked" | "not_checked"
    feedback: str = ""
    confidence: float = 0.0
    # Shown to the user ahead of ``feedback`` (never to the model): who judged,
    # when that is the session's own on-device model judging its own work.
    note: str = ""

    @property
    def is_achieved(self) -> bool:
        return self.verdict == "achieved"

    @property
    def shown_feedback(self) -> str:
        """``feedback`` as the user sees it: prefixed with ``note``."""
        return f"{self.note} {self.feedback}".strip() if self.note else self.feedback

    @property
    def is_blocked(self) -> bool:
        return self.verdict == "blocked"

    @property
    def is_not_checked(self) -> bool:
        return self.verdict == "not_checked"


def not_checked(reason: str) -> Verdict:
    """The verdict for a judge that could not run or could not be understood."""
    return Verdict(verdict="not_checked", feedback=reason, confidence=0.0)


@dataclass
class GateContext:
    """Everything the gate needs, assembled by each caller. Provider-neutral:
    ``messages`` is the caller's native history (Anthropic blocks or Responses
    items); the tail renderer flattens either to text."""

    session_id: str
    messages: list = field(default_factory=list)
    goal: str = ""
    provider: str = "anthropic"  # "anthropic" | "openai" | "local"
    model_id: str = ""
    # The session's chat_models key: the judge on-device, and what "main"
    # resolves to for a cloud session's goal_evaluator.
    model_key: str = ""
    workspace: str | None = None
    # The content of the reply being gated. The runner persists it only after
    # the decision, so ``messages`` does not hold it yet; the gate appends it
    # to a copy of ``messages`` that its checks and its judge all read.
    final_reply: list | str | None = None
    # A model with no tools cannot act on a "produce the file" nudge, so the
    # checks that ask for one stay quiet for it.
    tools_enabled: bool = True
    # Plan mode refuses every write, so the checks that ask for a file must
    # stay quiet when it is on.
    plan_mode: bool = False
    # Progress sink for a user-visible status line while the judge runs.
    # Thread-safe; set by run_gate_with_progress.
    announce: Callable[[str], None] | None = None
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
    "not_checked",
]
