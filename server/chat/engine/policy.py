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
    # Completion gate (Stop hooks + goal evaluator). Off for the local preset:
    # the gate's evaluator may call a cloud model, and a local turn must stay
    # fully offline (parity with the pre-engine local loop, which had no gate).
    completion_gate: bool = True


CHAT_POLICY = TurnPolicy()
LOCAL_POLICY = TurnPolicy(completion_gate=False)
