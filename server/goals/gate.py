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

    # ── Phase 1.5: claimed deliverables (on for every turn, goal or not) ─────
    # The reply says a file was saved or points at an artifact card; check the
    # file exists (non-empty) and the artifact call happened THIS turn. Twice
    # in real sessions neither was true and the user found out only by asking.
    # A miss is a block with the missing items named, under the same cap.
    if _flag_on("deliverable_check"):
        from server.goals.deliverables import check_claims

        try:
            claim_feedback = check_claims(ctx.messages, ctx.workspace)
        except Exception as e:  # noqa: BLE001 — a checker bug must never abort a turn
            log.warning("deliverable check failed (%s); skipping", e)
            claim_feedback = None
        if claim_feedback:
            if ctx.attempt >= cap:
                return GateDecision(
                    block=False,
                    frame={
                        "goal_cap_reached": {
                            "attempt": ctx.attempt,
                            "cap": cap,
                            "source": "deliverable",
                        }
                    },
                    source="cap",
                )
            log.info("completion gate: deliverable claim unmet; continuing the turn")
            return GateDecision(
                block=True,
                feedback=claim_feedback,
                frame={"stop_hook_block": {"reason": claim_feedback, "attempt": ctx.attempt + 1}},
                source="deliverable",
            )

    # ── Phase 1.7: verification evidence (every turn, goal or not) ──────────
    # Code was edited this turn but no test/lint/typecheck/build command ran
    # green afterwards: ask for the run (or an honest blocker) instead of
    # letting "done, should work" end the turn. Deterministic, no model call,
    # bounded to MAX_VERIFY_NUDGES per turn on top of the shared cap.
    if _flag_on("verify_on_stop"):
        from server.goals.verification import verify_on_stop_feedback

        try:
            verify_feedback = verify_on_stop_feedback(ctx.messages, ctx.workspace)
        except Exception as e:  # noqa: BLE001 - the ledger must never abort a turn
            log.warning("verify-on-stop check failed (%s); skipping", e)
            verify_feedback = None
        if verify_feedback:
            if ctx.attempt >= cap:
                return GateDecision(
                    block=False,
                    frame={
                        "goal_cap_reached": {"attempt": ctx.attempt, "cap": cap, "source": "verify"}
                    },
                    source="cap",
                )
            log.info("completion gate: code edited without fresh verification; continuing")
            return GateDecision(
                block=True,
                feedback=verify_feedback,
                frame={"stop_hook_block": {"reason": verify_feedback, "attempt": ctx.attempt + 1}},
                source="verify",
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

    # ── Phase 2a: quality gates (deterministic, before the LLM judge) ────────
    # A red gate is proof the goal is not met: its output tail becomes the
    # continuation feedback and the evaluator is not consulted. Every boundary
    # re-runs a failed gate; a gate that has failed GATE_MAX_RETRIES times in a
    # row pauses the goal rather than looping.
    gates = goal_store.get_gates(ctx.session_id)
    if gates:
        from server.goals.gates import GATE_MAX_RETRIES, format_failure, run_gates

        results = await asyncio.to_thread(run_gates, [g["command"] for g in gates], ctx.workspace)
        updated = goal_store.record_gate_results(
            ctx.session_id, {r.command: r.passed for r in results}
        )
        failed = [r for r in results if not r.passed]
        if failed:
            exhausted = [g for g in updated if g["failures"] >= GATE_MAX_RETRIES]
            if exhausted:
                reason = (
                    f"quality gate `{exhausted[0]['command']}` failed {GATE_MAX_RETRIES} times; "
                    "goal paused. Fix it manually, remove the gate, or set the goal again."
                )
                goal_store.pause_goal(ctx.session_id, reason)
                return GateDecision(
                    block=False,
                    frame={
                        "goal_eval": {
                            "verdict": "blocked",
                            "feedback": reason,
                            "confidence": 1.0,
                            "attempt": ctx.attempt,
                            "cap": cap,
                        }
                    },
                    source="gate",
                )
            feedback = format_failure(results)
            new_count = goal_store.record_block(ctx.session_id, "not_achieved", feedback[:400])
            return GateDecision(
                block=True,
                feedback=feedback,
                frame={"stop_hook_block": {"reason": feedback[:600], "attempt": new_count}},
                source="gate",
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
