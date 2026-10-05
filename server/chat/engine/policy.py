"""TurnPolicy — the single knob set for how long and how hard a turn runs.

Every engine consumer (interactive chat, subagents, cron) is a policy preset
over the same loop. How many rounds and how much time a run gets comes from
the one setting every run reads (server/infrastructure/run_limits.py).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnPolicy:
    # Hard cap on model rounds within one turn. None takes the round limit
    # from Settings when the turn starts; a run with its own budget passes it
    # (an agent's resolved config, a scheduled run's remaining rounds).
    max_rounds: int | None = None
    # Wall-clock brake; None = no deadline (chat and voice, where the user's
    # Stop button is the brake). Runs nobody is watching pass the Settings
    # time limit: agents, scheduled runs and unattended headless runs.
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
    cap = max_rounds + int(extension.get("rounds") or 0)
    if extension.get("finish"):
        return min(cap, int(extension.setdefault("finish_at", round_num + 1)))
    return cap
