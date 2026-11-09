"""How an agent run ended, in ONE vocabulary.

Every place that records or reports a run reads it from here: AgentResult,
the journal, the agent_tasks row and its task event, the live phase events,
and the text the parent model reads.

``stop_reason`` names why the run ended and is the only thing a run sets.
``run_status`` derives the coarse status from it, so the two can never
disagree:

- ``completed``: the run finished its work.
- ``stopped``: it ended before finishing, at a limit or by a cancel. Its
  output is partial: memory does not count the run as done, and a worktree
  is kept, not applied.
- ``failed``: an error ended it.
"""

from __future__ import annotations

from typing import Literal

RunStatus = Literal["completed", "stopped", "failed"]

# A limit reason means the run's budget ran out before it finished.
LIMIT_REASONS = ("turn_limit", "deadline", "cost_cap", "refused_calls")
STOP_REASONS = ("completed", *LIMIT_REASONS, "cancelled", "error")

# The task event a finished row emits (server/tasks/events.py).
TASK_EVENTS: dict[RunStatus, str] = {
    "completed": "task_completed",
    "stopped": "task_stopped",
    "failed": "task_failed",
}


def run_status(stop_reason: str | None) -> RunStatus:
    """The status a run that ended for ``stop_reason`` has. An unknown or
    missing reason counts as an error: a run never reads as finished by
    accident."""
    if stop_reason == "completed":
        return "completed"
    if stop_reason in LIMIT_REASONS or stop_reason == "cancelled":
        return "stopped"
    return "failed"


def hit_limit(stop_reason: str | None) -> bool:
    """Whether a limit (rounds, time, cost, refused calls) ended the run."""
    return stop_reason in LIMIT_REASONS


def known_reason(stop_reason: str | None) -> str:
    """``stop_reason`` when it is one of STOP_REASONS, else ``error``."""
    return stop_reason if stop_reason in STOP_REASONS else "error"
