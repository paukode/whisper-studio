"""The tool phase of a round: the model's calls, run through the safety gate.

The round loop (runner.py) streams one provider round; when that round ended
with tool calls, ``run_tool_round`` does everything between those calls and
the next round. It starts the batch (execute_tool_batch) and, while the batch
runs, relays the agent event bus to the client as team_progress frames; it
gates the finished states (process_tool_results, with the tool-result budget)
and sends the frames they produced; it stashes a turn that stopped for an
approval or a question in the paused store; and on the way to the next round
it appends the results to the message list, lets the claim guard settle the
sentences it held back, and compacts the conversation once it has grown past
the threshold. The loop keeps the exits: it ends the stream when the phase
reports a pause or no result to send back.

Every tool result flows through execute_tool_batch and process_tool_results
(server/tool_executor.py): this module relays what they return and never
reads tool output itself. The frames it yields belong to the SSE vocabulary
pinned by tests/golden_fixtures.

runner.py imports this module; nothing here imports the runner.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from server.utils import ndjson_dumps

from .context import TurnContext
from .pause import paused_sessions

log = logging.getLogger("whisper-studio")


@dataclass
class ToolRound:
    """What the round loop needs once the round's tool calls have run.
    ``run_tool_round`` fills it in as the phase progresses; it is complete
    when the generator is exhausted."""

    # The message list the next round continues from: the one handed in, or
    # the compacted list that replaced it.
    messages: list = field(default_factory=list)
    # The tool_result blocks the batch produced, in request order. Empty
    # means there is nothing to send back and the loop ends the stream.
    tool_results: list = field(default_factory=list)
    # An approval-gated tool or a question stopped the turn: its state is in
    # the paused store, and the loop ends the stream marked paused.
    paused: bool = False


def _route_event(ev: dict) -> dict | None:
    """Agent-runtime events become team_progress frames; typed events
    delivered by the long-lived session stream are skipped to prevent
    double delivery."""
    if ev.get("type") in (
        "cron_event",
        "memory_event",
        "task_event",
        "cron_progress",
        "ci_progress",
        "ci_result",
        "session_message",
        # A workflow's own events reach the UI two other ways: the run
        # card's per-run EventSource for live progress, and a task_event
        # (above) for the terminal outcome. Without this they were
        # relabelled as team_progress mid-batch, which renders a detached
        # workflow as if it were this turn's subagent team.
        "workflow_event",
    ):
        return None
    return {"team_progress": ev}


async def run_tool_round(
    ctx: TurnContext,
    messages: list,
    tool_uses: list,
    outcome: ToolRound,
    *,
    scope_id: str,
    round_num: int,
):
    """Yield the SSE strings of one round's tool phase.

    ``ctx`` is the turn's TurnContext. ``messages`` is the live
    message list, with the round's assistant content (the one holding
    ``tool_uses``) already appended. ``scope_id`` keys the turn-scoped
    stores (the paused store, the auto-mode breaker, the loop guard).
    ``outcome`` is filled in as the phase progresses. Closing the generator
    mid-phase (the turn torn down by a Stop or a closed tab) cancels a tool
    batch still running."""
    from server.agents.event_bus import event_bus as _agent_event_bus
    from server.chat.attachment_context import ensure_attachments_present
    from server.chat.budget import make_budget_tool_result
    from server.chat.compaction import (
        compact_messages_with_claude,
        estimate_message_size,
        thresholds_for,
    )
    from server.chat.tool_pool import _is_tool_concurrent_safe
    from server.tool_executor import execute_tool_batch, process_tool_results

    session_id = ctx.session_id
    outcome.messages = messages

    _agent_channel = ctx.event_channel or session_id
    _event_queue = _agent_event_bus.subscribe(_agent_channel)
    _batch_task = asyncio.create_task(
        execute_tool_batch(
            tool_uses,
            is_concurrent_safe=_is_tool_concurrent_safe,
            loop=ctx.loop,
            executor=ctx.executor,
            transcript=ctx.transcript,
            attachments=ctx.current_attachments,
            session_id=session_id,
            session_denials=ctx.session_denials,
            model_id=ctx.model_id if ctx.tool_exec_model_id is None else ctx.tool_exec_model_id,
            plan_mode=ctx.plan_mode,
            mode=ctx.mode,
            # Subagents inherit the turn's clamped effort so an
            # ultracode parent no longer fans out to plain children.
            effort_label=ctx.effort_label,
            unattended=ctx.unattended,
            guard_scope=scope_id,
            event_channel=ctx.event_channel,
            workspace_latch=ctx.ws_latch,
            tool_scope=ctx.tool_scope,
        )
    )
    try:
        try:
            while not _batch_task.done():
                try:
                    _ev = await asyncio.wait_for(_event_queue.get(), timeout=0.05)
                    _payload = _route_event(_ev)
                    if _payload is not None:
                        yield f"data: {ndjson_dumps(_payload)}\n\n"
                except asyncio.TimeoutError:
                    pass
            while not _event_queue.empty():
                _ev = _event_queue.get_nowait()
                _payload = _route_event(_ev)
                if _payload is not None:
                    yield f"data: {ndjson_dumps(_payload)}\n\n"
            states = _batch_task.result()
        finally:
            _agent_event_bus.unsubscribe(_agent_channel, _event_queue)

        truncation_events: list[dict] = []
        (
            tool_results,
            sse_events,
            has_pending_approval,
            has_user_question,
        ) = await process_tool_results(
            states,
            budget_fn=make_budget_tool_result(truncation_events),
            session_approvals=ctx.session_approvals,
            config=ctx.session_config,
            model_id=ctx.model_id if ctx.tool_exec_model_id is None else ctx.tool_exec_model_id,
            recent_messages=[m for m in messages if m.get("role") == "assistant"][-3:],
            mode=ctx.mode,
            # The auto-mode breaker keys on the turn scope (like the goal
            # store); the classifier and explainer bill to the chat session.
            session_id=scope_id,
            cost_session_id=session_id,
            unattended=ctx.unattended,
            # Pre-approved actions run in there, under the batch's latch.
            workspace_latch=ctx.ws_latch,
        )
        outcome.tool_results = tool_results

        for evt in sse_events:
            yield f"data: {evt}\n\n"
        for trunc_evt in truncation_events:
            yield f"data: {ndjson_dumps(trunc_evt)}\n\n"

        if has_user_question or has_pending_approval:
            # Stash the paused conversation so the continuation turn can
            # rebuild a well-formed request with placeholders replaced.
            paused_sessions[scope_id] = {
                "messages": list(messages),
                "pending_tool_results": list(tool_results),
                "provider": ctx.adapter.provider,
                "asked": ctx.asked,
            }
            outcome.paused = True
            return

        log.info("Round %d: %d tool results, continuing loop", round_num, len(tool_results))

        if not tool_results:
            return

        messages.append({"role": "user", "content": tool_results})
        for _frame in ctx.claims.after_tools(messages):
            yield _frame

        # Proactive compaction: char estimate, supplemented by TOKEN truth
        # (the per-round usage crossed 80% of the window).
        from server.chat.loop_hints import should_nudge_compaction

        if estimate_message_size(messages) > thresholds_for(ctx.model_key)[
            0
        ] or should_nudge_compaction(scope_id):
            # An LLM summarization call sits between rounds here; without a
            # frame the wait reads as a mid-turn hang.
            yield f"data: {ndjson_dumps({'status': 'Compacting conversation context...'})}\n\n"
            messages = await compact_messages_with_claude(
                messages, ctx.model_id, session_id=session_id, model_key=ctx.model_key
            )
            # Restore any attachment the summary swallowed. The REACTIVE
            # path (the prompt-too-long rescue in runner.py) deliberately
            # does not re-inject (it must shrink; analyze_document has
            # session-wide access as the fallback).
            messages = ensure_attachments_present(messages, session_id)
        outcome.messages = messages
    finally:
        # Torn down mid-phase (Stop / tab-close): cancel a tool batch still
        # running. A batch that finished is left alone.
        if not _batch_task.done():
            _batch_task.cancel()
