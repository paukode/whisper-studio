"""The completion gate — one decision function every loop calls at end-of-turn.

Order: (1) WS-I Stop hooks (deterministic, cheap; reuses the engine already
wired into every loop), then (2) the goal evaluator (only if a goal is active).
A block means: do not end the turn — inject the feedback and loop again. The
consecutive-block cap (default 8) backstops both a stuck evaluator and a
misconfigured always-blocking Stop hook.

Pure decision logic: no SSE, no FastAPI. The three chat loops, cron, and WS-D
all call ``run_completion_gate`` and act on the returned ``GateDecision``.
"""

from __future__ import annotations

import asyncio
import logging

from server.goals import (
    CONFIDENT_BLOCK_THRESHOLD,
    GateContext,
    GateDecision,
)
from server.goals import store as goal_store

log = logging.getLogger("whisper-studio")


def _flag_on(name: str, default: bool = True) -> bool:
    try:
        from server.infrastructure.feature_flags import is_enabled

        return is_enabled(name)
    except Exception:
        return default


def _max_blocks(ctx: GateContext) -> int:
    try:
        from server.infrastructure import config

        return int(config.get("goal_max_consecutive_blocks", ctx.max_consecutive_blocks))
    except Exception:
        return ctx.max_consecutive_blocks


async def run_completion_gate(ctx: GateContext) -> GateDecision:
    """Decide whether the turn may end. See module docstring for ordering.

    The goal_loop flag gates ONLY the evaluator phase — Stop hooks are WS-I's
    contract and must keep firing even when the goal loop is disabled (the gate
    replaced the loops' direct check_stop_hooks calls)."""
    cap = _max_blocks(ctx)

    # ── Phase 1: Stop hooks (WS-I engine) ────────────────────────────────────
    from server.hooks import check_stop_hooks

    stop = await check_stop_hooks(
        ctx.session_id,
        ctx.workspace,
        stop_hook_active=ctx.attempt > 0,
        model_id=ctx.model_id,
    )
    if stop.blocked:
        if ctx.attempt >= cap:
            return GateDecision(
                block=False,
                frame={
                    "goal_cap_reached": {"attempt": ctx.attempt, "cap": cap, "source": "stop_hook"}
                },
                source="cap",
            )
        return GateDecision(
            block=True,
            feedback=stop.reason,
            frame={"stop_hook_block": {"reason": stop.reason, "attempt": ctx.attempt + 1}},
            source="stop_hook",
        )

    # ── Phase 2: goal evaluator (only if the flag is on and a goal is active) ─
    if not _flag_on("goal_loop"):
        return GateDecision(block=False)
    goal = ctx.goal or goal_store.get_goal(ctx.session_id)["goal"]
    if not goal or not goal_store.is_active(ctx.session_id):
        return GateDecision(block=False)

    if ctx.attempt >= cap:
        goal_store.record_pass(ctx.session_id, "not_achieved", "hit consecutive-block cap")
        return GateDecision(
            block=False,
            frame={"goal_cap_reached": {"attempt": ctx.attempt, "cap": cap, "source": "evaluator"}},
            source="cap",
        )

    from server.goals import Verdict
    from server.goals.evaluator import evaluate

    # evaluate() makes a blocking one_shot call — keep it off the event loop.
    # provider is keyword-only on evaluate(); fail OPEN on any unexpected error
    # so a broken evaluator can never abort the turn.
    try:
        verdict = await asyncio.to_thread(evaluate, goal, ctx.messages, provider=ctx.provider)
    except Exception as e:
        log.warning("Goal evaluator failed (%s); allowing turn to end.", e)
        verdict = Verdict(verdict="achieved", feedback="(evaluator error)", confidence=0.0)

    if verdict.is_achieved:
        goal_store.record_pass(ctx.session_id, "achieved", verdict.feedback)
        return GateDecision(
            block=False,
            frame={
                "goal_eval": {
                    "verdict": "achieved",
                    "feedback": verdict.feedback,
                    "confidence": verdict.confidence,
                    "attempt": ctx.attempt,
                    "cap": cap,
                }
            },
            goal_achieved=True,
            source="evaluator",
        )

    # A confident 'blocked' verdict ends the turn and surfaces the blocker
    # instead of looping against something outside the agent's control.
    if verdict.is_blocked and verdict.confidence >= CONFIDENT_BLOCK_THRESHOLD:
        goal_store.record_pass(ctx.session_id, "blocked", verdict.feedback)
        return GateDecision(
            block=False,
            frame={
                "goal_eval": {
                    "verdict": "blocked",
                    "feedback": verdict.feedback,
                    "confidence": verdict.confidence,
                    "attempt": ctx.attempt,
                    "cap": cap,
                }
            },
            source="evaluator",
        )

    # not_achieved (or low-confidence blocked): keep working toward the goal.
    new_count = goal_store.record_block(ctx.session_id, verdict.verdict, verdict.feedback)
    feedback = (
        f"{verdict.feedback} Continue working toward the goal; end the turn only "
        "when it is genuinely achieved or you are hard-blocked."
    )
    return GateDecision(
        block=True,
        feedback=feedback,
        frame={
            "goal_eval": {
                "verdict": verdict.verdict,
                "feedback": verdict.feedback,
                "confidence": verdict.confidence,
                "attempt": new_count,
                "cap": cap,
            }
        },
        source="evaluator",
    )


def build_context_from_text(
    goal: str, tail_text: str, provider: str, *, session_id: str = ""
) -> GateContext:
    """Convenience for WS-D: gate a workflow's completion against a pre-rendered
    journal tail without importing chat internals. Wraps the tail as a single
    synthetic assistant message so the evaluator's renderer handles it."""
    return GateContext(
        session_id=session_id,
        messages=[{"role": "assistant", "content": tail_text}],
        goal=goal,
        provider=provider,
    )
