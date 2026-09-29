"""TurnPolicy — the single knob set for how long and how hard a turn runs.

Every engine consumer (interactive chat, subagents, cron) is a policy preset
over the same loop, so "how many rounds does an agent get" is decided here
and nowhere else.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnPolicy:
    # Hard cap on model rounds within one turn.
    max_rounds: int = 50
    # Wall-clock brake; None = no deadline (interactive chat, where the
    # user's Stop button is the brake). No in-tree preset sets this yet —
    # it is reserved for the planned cron/agents port onto this engine
    # (both currently run their own loops with their own deadlines).
    deadline_seconds: float | None = None
    # Whether an unrescuable turn gets one final no-tools round on a hard-
    # trimmed context (synthesize from what remains) instead of a bare error.
    salvage_round: bool = True
    # Completion gate (Stop hooks, the deliverable and verification checks, the
    # goal's quality gates and its judge). Unattended consumers (agents, cron,
    # headless runs) build their own policy with it off.
    completion_gate: bool = True
    # Run the gate only while the session has an active goal (the goal_loop
    # flag on and a goal set). The on-device preset: every block costs a full
    # local round, so plain on-device chat keeps ending when the model stops,
    # and a goal brings the whole gate, judged on the session's own model.
    gate_requires_goal: bool = False


CHAT_POLICY = TurnPolicy()
LOCAL_POLICY = TurnPolicy(gate_requires_goal=True)


def round_cap(extension: dict, max_rounds: int, round_num: int) -> int:
    """This round's cap on the turn: the policy's rounds plus any live grant
    (server.agents.extensions). Once the run is told to finish (its "finish"
    flag), the cap stops at the round after the one that saw the flag, so
    that round is the last and the loop ends after it."""
    if extension.get("finish"):
        return int(extension.setdefault("finish_at", round_num + 1))
    return max_rounds + int(extension.get("rounds") or 0)
