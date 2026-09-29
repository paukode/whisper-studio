"""The completion gate: one decision function the engine calls at end of turn.

Order: (1) WS-I Stop hooks, (1.5 to 1.7) the deterministic deliverable,
requested-file and verification checks, then, only while a goal is active,
(2a) the goal's quality gates and (2b) the judge. The checks and the judge
read one transcript: the history with the reply being gated at its end. A
block means: do not end the turn, inject the feedback and loop again. The
consecutive-block cap (default 8) backstops both a stuck judge and an
always-blocking Stop hook.

Which turns reach the gate is the caller's TurnPolicy: every interactive
cloud turn, and an on-device turn only while a goal is active. The judge is
the goal_evaluator model for a cloud session and the session's own resident
model on-device (server.goals.local_judge). A judge that cannot run or cannot
be understood gives ``not_checked``: the turn ends, the goal stays set, and it
is never recorded as achieved.

Pure decision logic: no SSE, no FastAPI. ``run_gate_with_progress`` adds the
judge's status line for a streaming caller.
"""

from __future__ import annotations

import asyncio
import logging

from server.goals import (
    CONFIDENT_BLOCK_THRESHOLD,
    GateContext,
    GateDecision,
    Verdict,
    not_checked,
)
from server.goals import store as goal_store

log = logging.getLogger("whisper-studio")


# What the chat shows while the model is sent back over a claim that did not
# hold (server/chat/claim_guard.py held the sentence back).
CLAIM_HELD_REASON = (
    "Held back a sentence claiming something was delivered, because it could not be "
    "verified. The model is doing the work now or will say it was not done."
)


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


def goal_in_play(session_id: str) -> bool:
    """Whether the gate's goal phases run for ``session_id``: the goal_loop
    flag is on and the session has an active goal."""
    return _flag_on("goal_loop") and goal_store.is_active(session_id)


def last_round_not_checked(
    session_id: str,
    *,
    salvage: bool = False,
    budget: bool = False,
    deadline: bool = False,
    attempt: int,
    cap: int,
) -> dict | None:
    """The ``goal_eval`` frame for a turn that ends on its last round with a
    goal in play, else None.

    The gate does not run there: a block would need a round the turn does not
    have. So an active goal is recorded as not checked, with the reason the
    round was the last (a salvage round after a context overflow, the cost
    budget, the time budget, else the round cap), instead of the turn ending
    with no verdict at all. The goal stays set and is checked when the next
    turn ends."""
    if not goal_in_play(session_id):
        return None
    if salvage:
        why = (
            "the conversation no longer fit the model's context window, so the "
            "final answer was written from what fit"
        )
    elif budget:
        why = "the cost budget made this the turn's final round"
    elif deadline:
        why = "the turn's time budget made this its final round"
    else:
        why = "the turn used its last round"
    reason = (
        f"The goal was not checked: {why}. "
        "The goal stays set and is checked again when the next turn ends."
    )
    goal_store.record_not_checked(session_id, reason)
    return {
        "goal_eval": {
            "verdict": "not_checked",
            "feedback": reason,
            "confidence": 0.0,
            "attempt": attempt,
            "cap": cap,
        }
    }


def _hook_model_id(ctx: GateContext) -> str:
    """The model id a Stop hook's payload names: the ``local:...`` chat_models
    id on-device (what the local memory hooks pass), the provider id on cloud."""
    if ctx.provider == "local":
        from server.local.runtime import local_model_meta

        return local_model_meta(ctx.model_key).get("id") or ctx.model_id
    return ctx.model_id


async def _session_has_artifact(session_id: str) -> bool:
    """Whether the session already holds an artifact card, so a reply about a
    card an earlier turn made is not a false claim. server.artifacts keeps
    this process's record of every card and falls back to the programArtifact
    rows the session's saved history carries, so the answer survives an app
    restart and a session reload. That fallback is a database read, so it
    runs off the event loop; a lookup that fails counts as no card, which
    leaves the check as strict as a turn-only one."""
    if not session_id:
        return False
    from server.artifacts import list_artifacts

    try:
        return bool(await asyncio.to_thread(list_artifacts, session_id))
    except Exception as e:  # noqa: BLE001 - a lookup bug must never abort a turn
        log.warning("artifact lookup for the deliverable check failed (%s)", e)
        return False


def _gated_messages(ctx: GateContext) -> list:
    """The transcript the checks and the judge read: the history with the
    reply being gated at its end. The runner persists that reply only after
    the decision, so the last assistant row of ``ctx.messages`` is the round
    before it. A new list: the caller's history is never changed."""
    messages = list(ctx.messages)
    if ctx.final_reply:
        messages.append({"role": "assistant", "content": ctx.final_reply})
    return messages


async def run_completion_gate(ctx: GateContext) -> GateDecision:
    """Decide whether the turn may end. See module docstring for ordering.

    The goal_loop flag gates ONLY the goal phases: Stop hooks are WS-I's
    contract and must keep firing even when the goal loop is disabled (the gate
    replaced the loops' direct check_stop_hooks calls)."""
    cap = _max_blocks(ctx)

    # ── Phase 1: Stop hooks (WS-I engine) ────────────────────────────────────
    from server.hooks import check_stop_hooks

    stop = await check_stop_hooks(
        ctx.session_id,
        ctx.workspace,
        stop_hook_active=ctx.attempt > 0,
        model_id=_hook_model_id(ctx),
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

    # Every phase from here judges the same transcript, the reply included.
    messages = _gated_messages(ctx)

    # ── Phase 1.5: claimed deliverables (every gated turn, goal or not) ─────
    # The reply says a file was saved or points at an artifact card; check the
    # file exists (non-empty) and the card was made, this turn or by an earlier
    # one of the session. Twice in real sessions neither was true and the user
    # found out only by asking. A miss is a block with the missing items
    # named, at most MAX_CLAIM_NUDGES per turn under the same cap; plan mode
    # checks paths only once a tool that writes outside the workspace ran,
    # since until then they are files the plan will write. A model with no
    # tools cannot produce the file, so it is not asked to.
    if ctx.tools_enabled and _flag_on("deliverable_check"):
        from server.goals.deliverables import artifact_claim_unmet, check_claims

        try:
            has_artifact = artifact_claim_unmet(messages) and await _session_has_artifact(
                ctx.session_id
            )
            claim_feedback = check_claims(
                messages,
                ctx.workspace,
                plan_mode=ctx.plan_mode,
                session_has_artifact=has_artifact,
                started_at=ctx.turn_started_at,
                calls=ctx.claim_calls,
                receipts=ctx.claim_receipts,
            )
        except Exception as e:  # noqa: BLE001 - a checker bug must never abort a turn
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
            # The user never saw the held sentence, so the card must not
            # repeat it: the feedback quoting it goes to the model only.
            return GateDecision(
                block=True,
                feedback=claim_feedback,
                frame={
                    "stop_hook_block": {
                        "reason": CLAIM_HELD_REASON,
                        "attempt": ctx.attempt + 1,
                        "source": "deliverable",
                    }
                },
                source="deliverable",
            )

    # ── Phase 1.6: requested deliverable (every gated turn, goal or not) ───
    # The mirror of phase 1.5: the user asked for a file and the turn produced
    # none. A real session answered "update the diagram and save it to
    # Downloads" with a revised description in chat and no file, and the user
    # only found out by asking. One nudge per turn, since the ask is read out
    # of prose. Plan mode is exempt: the workspace writes are refused there by
    # design, and so is a model with no tools, which has no file tool to call.
    if ctx.tools_enabled and _flag_on("requested_file_check"):
        from server.goals.requested_files import requested_file_feedback

        try:
            file_feedback = requested_file_feedback(
                messages, ctx.workspace, plan_mode=ctx.plan_mode
            )
        except Exception as e:  # noqa: BLE001 - a checker bug must never abort a turn
            log.warning("requested-file check failed (%s); skipping", e)
            file_feedback = None
        if file_feedback:
            if ctx.attempt >= cap:
                return GateDecision(
                    block=False,
                    frame={
                        "goal_cap_reached": {
                            "attempt": ctx.attempt,
                            "cap": cap,
                            "source": "requested_file",
                        }
                    },
                    source="cap",
                )
            log.info("completion gate: a requested file was not produced; continuing the turn")
            return GateDecision(
                block=True,
                feedback=file_feedback,
                frame={"stop_hook_block": {"reason": file_feedback, "attempt": ctx.attempt + 1}},
                source="requested_file",
            )

    # ── Phase 1.7: verification evidence (every gated turn, goal or not) ────
    # Code was edited this turn but no test/lint/typecheck/build command ran
    # green afterwards: ask for the run (or an honest blocker) instead of
    # letting "done, should work" end the turn. Deterministic, no model call,
    # bounded to MAX_VERIFY_NUDGES per turn on top of the shared cap.
    if _flag_on("verify_on_stop"):
        from server.goals.verification import verify_on_stop_feedback

        try:
            verify_feedback = verify_on_stop_feedback(messages, ctx.workspace)
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

    # ── Phase 2b: the judge ──────────────────────────────────────────────────
    verdict = await _judge(ctx, goal, messages)

    if verdict.is_not_checked:
        reason = verdict.feedback.rstrip()
        if not reason.endswith((".", "!", "?")):
            reason += "."
        reason += " The goal stays set and is checked again when the next turn ends."
        goal_store.record_not_checked(ctx.session_id, reason)
        return GateDecision(
            block=False,
            frame={
                "goal_eval": {
                    "verdict": "not_checked",
                    "feedback": reason,
                    "confidence": 0.0,
                    "attempt": ctx.attempt,
                    "cap": cap,
                }
            },
            source="evaluator",
        )

    if verdict.is_achieved:
        goal_store.record_pass(ctx.session_id, "achieved", verdict.shown_feedback)
        return GateDecision(
            block=False,
            frame={
                "goal_eval": {
                    "verdict": "achieved",
                    "feedback": verdict.shown_feedback,
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
        goal_store.record_pass(ctx.session_id, "blocked", verdict.shown_feedback)
        return GateDecision(
            block=False,
            frame={
                "goal_eval": {
                    "verdict": "blocked",
                    "feedback": verdict.shown_feedback,
                    "confidence": verdict.confidence,
                    "attempt": ctx.attempt,
                    "cap": cap,
                }
            },
            source="evaluator",
        )

    # not_achieved (or low-confidence blocked): keep working toward the goal.
    new_count = goal_store.record_block(ctx.session_id, verdict.verdict, verdict.shown_feedback)
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
                "feedback": verdict.shown_feedback,
                "confidence": verdict.confidence,
                "attempt": new_count,
                "cap": cap,
            }
        },
        source="evaluator",
    )


async def _judge(ctx: GateContext, goal: str, messages: list) -> Verdict:
    """Phase 2b's verdict on ``messages``, the transcript the checks before it
    read, the reply being gated included (``_gated_messages``). Any exception
    is ``not_checked``, never a pass."""
    try:
        if ctx.provider == "local":
            from server.goals.local_judge import evaluate_on_device

            return await evaluate_on_device(
                goal, messages, model_key=ctx.model_key, announce=ctx.announce
            )
        from server.goals.evaluator import evaluate

        # evaluate() blocks on a one_shot call: keep it off the event loop.
        return await asyncio.to_thread(
            evaluate,
            goal,
            messages,
            main_model_key=ctx.model_key,
            announce=ctx.announce,
            session_id=ctx.session_id,
        )
    except Exception as e:  # noqa: BLE001 - reported to the user, never a pass
        log.warning("Goal judge failed (%s); the goal is not checked this turn.", e)
        return not_checked(f"The goal check failed ({type(e).__name__}: {e})")


async def run_gate_with_progress(ctx: GateContext):
    """Run the gate for a streaming caller. Yields a ``{"status": text}``
    frame for each progress line the judge announces while it runs, then the
    GateDecision last.

    The judge runs after the reply has streamed and can take minutes
    on-device, so the user is told which model is checking. The gate runs as
    a task so its announcements reach the stream while it is still awaited; a
    consumer that stops early (Stop, a closed tab) cancels it."""
    loop = asyncio.get_running_loop()
    statuses: asyncio.Queue[str] = asyncio.Queue()
    ctx.announce = lambda text: loop.call_soon_threadsafe(statuses.put_nowait, text)
    task = asyncio.ensure_future(run_completion_gate(ctx))
    getter: asyncio.Future | None = None
    try:
        while not task.done():
            getter = asyncio.ensure_future(statuses.get())
            await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
            if getter.done():
                yield {"status": getter.result()}
            else:
                getter.cancel()
        yield task.result()
    finally:
        for pending in (getter, task):
            if pending is not None and not pending.done():
                pending.cancel()
        # An abandoned gate's outcome is nobody's: retrieve it so a late
        # exception is not logged as never retrieved.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
