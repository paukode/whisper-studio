"""The unified turn loop.

``run_turn`` is the one agentic loop every provider and consumer shares. It
consumes neutral round events from a provider adapter and owns everything that
must behave identically across Claude/GPT/local and chat/subagents/cron:

  - round budgets and wind-down reminders (TurnPolicy)
  - context management: proactive compaction (char estimate + token-truth
    nudge), reactive prompt-too-long rescue (trim + retry), and the salvage
    round (one final no-tools round on a hard-trimmed context, so a turn that
    did forty rounds of work never dies with a bare error)
  - tool execution through the shared safety gate (execute_tool_batch +
    process_tool_results — every result flows the gate, the engine never reads
    tool output directly)
  - approval / ask_user pauses via the unified pause store
  - completion gate (Stop hooks + goal evaluator), per-round cost accounting,
    and the post-turn memory hooks

The SSE frame vocabulary is pinned by tests/golden_fixtures — change frames
only with an intentional GOLDEN_RECORD refresh.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.utils import BoundedUUIDSet, ndjson_dumps

from .events import (
    Heartbeat,
    Incomplete,
    RoundError,
    RoundResult,
    TextDelta,
    ThinkingDelta,
    ThinkingStart,
    ThinkingStop,
    ToolCall,
    ToolCallStart,
)
from .pause import paused_sessions
from .policy import TurnPolicy

log = logging.getLogger("whisper-studio")


def strip_partial_tool_use(content: list[dict]) -> list[dict]:
    """Prepare an assistant turn for re-injection without a tool_result.

    ``max_tokens`` (or a completion-gate loop) can leave partial ``tool_use``
    blocks in the assistant turn; feeding them back without matching
    tool_results is a non-retryable provider error. Drop them, keeping the
    text/thinking, and never return an empty turn."""
    if not any(b.get("type") == "tool_use" for b in content):
        return content
    kept = [b for b in content if b.get("type") != "tool_use"]
    return kept or [{"type": "text", "text": "(continuing)"}]


@dataclass
class TurnContext:
    """Everything a turn needs, pre-assembled by the route (or agent/cron
    caller). The engine does no request parsing, grounding, or prompt
    building — it runs the loop."""

    session_id: str
    model_key: str
    model_id: str
    messages: list
    adapter: Any
    policy: TurnPolicy
    loop: Any
    executor: Any
    plan_mode: bool = False
    mode: str = "default"
    ws_path: str | None = None
    suppress_ws_search: bool = False
    effort_label: str | None = None
    transcript: str = ""
    current_attachments: dict = field(default_factory=dict)
    session_denials: dict = field(default_factory=dict)
    session_approvals: dict = field(default_factory=dict)
    session_config: dict = field(default_factory=dict)
    advertised_count: int = 0
    deferred_count: int = 0
    deferred_tokens_est: int = 0
    is_new_turn: bool = True
    # Model id handed to the tool executor / permission pipeline. None means
    # ctx.model_id; the local path passes "" so the permission explainer and
    # auto-mode classifier stay offline (they gate on a truthy model id).
    tool_exec_model_id: str | None = None
    # Post-turn memory hooks override; None uses the engine's generic hooks.
    # The local path supplies its own (offline-aware model_mode gating).
    memory_hooks: Callable[[list], None] | None = None
    # Callbacks into route-owned state (None outside interactive chat).
    heartbeat: Callable[[], None] | None = None
    is_disconnected: Callable[[], Any] | None = None  # async
    # True for a turn with no human attending it (subagents today; any future
    # unattended caller). Threaded into execute_tool_batch/process_tool_results
    # so approval-gated and pause-inducing tools resolve without a human:
    # WS_APPROVAL executes inline, WS_WORKSPACE_PROMPT and ask_user_question
    # refuse instead of pausing, and every dispatched call is stamped
    # __agent__ (mirrors server.agents.runtime._with_session_id).
    unattended: bool = False
    # Alternate key for turn-scoped, session_id-keyed global state (goal
    # store, the auto-mode circuit breaker, the paused-turn store, and the
    # loop-hints context tracker). None (every existing caller today) means
    # "key on session_id exactly as before" — byte-identical behavior. Set
    # this when the real session_id is shared with a concurrently in-flight
    # turn (e.g. a subagent's inner turn running while the parent chat turn
    # that spawned it is still streaming): without it, the subagent's turn
    # would reset the PARENT's in-flight goal tracking / auto-mode breaker
    # and collide with its paused-approval slot.
    turn_scope_id: str | None = None
    # Per-round tool catalog override. When set, called instead of
    # _assemble_round_tools(ctx) to get (tools, core_count) — lets a caller
    # (e.g. the agent runtime) supply its own filtered/precomputed tool pool
    # without the engine reaching into chat-specific tool_pool/tool_partition
    # modules. None (every existing caller today) keeps current behavior.
    tool_catalog: Callable[[], tuple[list[dict], int | None]] | None = None


def _assemble_round_tools(ctx: TurnContext) -> tuple[list, int | None]:
    """Per-round tool catalog with progressive disclosure — reassembled every
    round so a tool_search activation in round N is advertised in round N+1."""
    from server.chat.tool_partition import partition_pool
    from server.chat.tool_pool import assemble_full_catalog
    from server.infrastructure.effort import is_ultracode
    from server.infrastructure.feature_flags import is_enabled

    catalog = assemble_full_catalog(
        plan_mode=ctx.plan_mode,
        ws_connected=bool(ctx.ws_path),
        suppress_workspace_search=ctx.suppress_ws_search,
        # The workflow runtime + CI tools live behind this one flag (the only
        # ultracode gate in tool_pool). THIS catalog becomes the request's
        # tools array, so omitting the flag here kept workflow_run off the wire
        # on every interactive round: an ultracode turn was handed a system
        # prompt ordering it to "write a workflow script and run it with
        # workflow_run" (server/prompts/ultracode.py) while the tool itself was
        # never advertised, leaving spawn_agent as the only orchestration it
        # could actually call. The route builds its own ultracode-aware pool,
        # but only for the deferred index and the SSE counts — never for this.
        # Callers that supply their own tool_catalog (cron, agents, headless)
        # never reach here, so nested workflows stay impossible.
        ultracode=is_ultracode(ctx.effort_label),
    )
    if is_enabled("progressive_tools"):
        from server.chat.tool_activation import get_ordered

        tools, _, core_count = partition_pool(catalog, get_ordered(ctx.session_id))
        return tools, core_count
    return catalog, None


async def run_turn(ctx: TurnContext):
    """Yield SSE strings for one full agentic turn."""
    from server.chat.attachment_context import ensure_attachments_present
    from server.chat.budget import make_budget_tool_result
    from server.chat.compaction import (
        compact_messages_with_claude,
        ensure_valid_start,
        estimate_message_size,
        sanitize_tool_pairs,
        thresholds_for,
    )
    from server.chat.infra import _estimate_cost
    from server.chat.tool_pool import _is_tool_concurrent_safe
    from server.costs.tracker import prompt_token_total
    from server.costs.tracker import record_turn as _record_cost_turn
    from server.infrastructure.errors import PromptTooLongError, WhisperAPIError
    from server.tool_executor import execute_tool_batch, process_tool_results

    messages = ctx.messages
    session_id = ctx.session_id
    # Turn-scoped global state (goal store, auto-mode breaker, the paused-turn
    # store, the loop-hints context tracker) keys on this instead of the bare
    # session_id whenever the caller set one — see TurnContext.turn_scope_id.
    _scope_id = ctx.turn_scope_id or session_id
    max_rounds = ctx.policy.max_rounds
    deadline = (
        (time.monotonic() + ctx.policy.deadline_seconds) if ctx.policy.deadline_seconds else None
    )
    # Set once when a deadline-hit round has been given its forced-empty-tools
    # treatment and finalize-now reminder, so a subsequent round (e.g. a
    # max_tokens continuation) doesn't re-inject the same reminder every time.
    _deadline_finalized = False

    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read = 0
    total_cache_creation = 0
    # Prompt tokens actually sent, summed over rounds and normalized across the
    # two cache-reporting conventions (see prompt_token_total). This is the only
    # one of these that means "tokens in" to a reader: total_input_tokens counts
    # just the UNCACHED remainder, which with caching on is near zero.
    total_prompt_tokens = 0

    # Completion gate (WS-E): how many times the gate forced this turn to keep
    # going. Capped so a stuck check can't loop forever.
    stop_blocks_used = 0
    from server.goals import DEFAULT_MAX_CONSECUTIVE_BLOCKS
    from server.goals import store as _goal_store

    _goal_cap = DEFAULT_MAX_CONSECUTIVE_BLOCKS
    try:
        from server.infrastructure import config as _cfg

        _goal_cap = int(_cfg.get("goal_max_consecutive_blocks", _goal_cap))
    except Exception:
        pass
    _goal_row = _goal_store.get_goal(_scope_id)
    goal_text = _goal_row["goal"]
    if ctx.is_new_turn and goal_text:
        _goal_store.reset_for_new_turn(_scope_id)

    # Auto-mode circuit breaker (server.security.permissions) is turn-scoped:
    # a breaker tripped last turn must not carry into this one.
    if ctx.is_new_turn:
        from server.security.permissions import reset_auto_mode_breaker

        reset_auto_mode_breaker(_scope_id)

    # Replay protection — skip duplicate tool_use IDs across the stream.
    _seen_tool_ids = BoundedUUIDSet(capacity=256)

    # Reactive prompt-too-long rescue state.
    salvage_mode = False

    if ctx.deferred_count:
        yield f"data: {ndjson_dumps({'tool_pool': {'advertised': ctx.advertised_count, 'deferred': ctx.deferred_count, 'total': ctx.advertised_count + ctx.deferred_count, 'deferred_tokens_est': ctx.deferred_tokens_est}})}\n\n"

    for round_num in range(max_rounds):
        deadline_hit = deadline is not None and time.monotonic() >= deadline
        is_last_round = round_num == max_rounds - 1 or deadline_hit or salvage_mode

        # Wind-down: tell the model when the round cap is near so it
        # consolidates instead of getting cut off mid-plan. Persisted into
        # history on purpose — a request-only injection would fork the token
        # prefix and break the moving cache checkpoint.
        from server.chat.loop_hints import inject_reminder, near_cap_reminder

        _reminder = near_cap_reminder(max_rounds - round_num)
        if _reminder and inject_reminder(messages, _reminder):
            log.info("Injected near-cap reminder (%d rounds left)", max_rounds - round_num)

        # Deadline enforcement: the FIRST round observed past the wall-clock
        # deadline gets a "finalize now" reminder, exactly once (a later
        # max_tokens/pause_turn continuation must not repeat it every round).
        # Tool availability is forced off below (parallel to salvage_mode) so
        # this round is genuinely terminal — tools-wise — regardless of
        # provider, rather than merely continuing with is_last_round=True.
        if deadline_hit and not _deadline_finalized:
            _deadline_finalized = True
            _deadline_reminder = (
                "<system-reminder>Time is up for this turn. Finalize your "
                "answer now with what you have; further tool calls will not "
                "execute.</system-reminder>"
            )
            if inject_reminder(messages, _deadline_reminder):
                log.info(
                    "Deadline hit — injected finalize-now reminder and forced a no-tools round"
                )

        # Heartbeat the stream slot so a long multi-round turn is never
        # mistaken for an abandoned stream.
        if ctx.heartbeat:
            ctx.heartbeat()

        # Client gone (tab closed / hard refresh): stop promptly.
        if ctx.is_disconnected is not None and await ctx.is_disconnected():
            log.info("Client disconnected mid-stream (session %s); ending", session_id)
            return

        # Cost budget check before each round.
        from server.costs.budget import check_budget

        budget_exceeded = check_budget(session_id)
        if budget_exceeded:
            yield f"data: {ndjson_dumps({'budget_warning': budget_exceeded.message, 'budget_kind': budget_exceeded.kind, 'budget_limit': budget_exceeded.limit, 'budget_current': budget_exceeded.current})}\n\n"
            yield f"data: {ndjson_dumps({'text': f'[Budget exceeded] {budget_exceeded.message}'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        if salvage_mode or deadline_hit:
            tools, core_count = [], None
        elif ctx.tool_catalog is not None:
            tools, core_count = ctx.tool_catalog()
        else:
            tools, core_count = _assemble_round_tools(ctx)

        _batch_task: asyncio.Task | None = None
        try:
            # ── One model round via the provider adapter ─────────────────────
            try:
                round_events = ctx.adapter.stream_round(
                    messages, tools, core_count, round_num, is_last_round
                )
                round_result: RoundResult | None = None
                errored = False
                async for ev in round_events:
                    if isinstance(ev, TextDelta):
                        yield f"data: {ndjson_dumps({'text': ev.text})}\n\n"
                    elif isinstance(ev, ThinkingStart):
                        yield f"data: {ndjson_dumps({'thinking_start': True})}\n\n"
                    elif isinstance(ev, ThinkingDelta):
                        yield f"data: {ndjson_dumps({'thinking': ev.text})}\n\n"
                    elif isinstance(ev, ThinkingStop):
                        yield f"data: {ndjson_dumps({'thinking_stop': True})}\n\n"
                    elif isinstance(ev, ToolCallStart):
                        yield f"data: {ndjson_dumps({'skill': ev.name, 'input': {}})}\n\n"
                    elif isinstance(ev, ToolCall):
                        yield f"data: {ndjson_dumps({'skill_input': ev.name, 'input': ev.input})}\n\n"
                    elif isinstance(ev, Heartbeat):
                        yield ": hb\n\n"
                    elif isinstance(ev, Incomplete):
                        _trunc_note = "\n\n*(Response truncated: output token limit reached.)*"
                        yield f"data: {ndjson_dumps({'text': _trunc_note})}\n\n"
                    elif isinstance(ev, RoundError):
                        yield f"data: {ndjson_dumps({'error': ev.message})}\n\n"
                        yield "data: [DONE]\n\n"
                        errored = True
                        break
                    elif isinstance(ev, RoundResult):
                        round_result = ev
                if errored:
                    return
            except PromptTooLongError:
                # Reactive rescue: strip oldest messages, compact, retry.
                log.warning("Prompt too long — applying reactive compaction")
                yield f"data: {ndjson_dumps({'status': 'Compacting context (prompt too long)...'})}\n\n"
                if len(messages) > 4:
                    messages = ensure_valid_start(messages[2:])
                    messages = await compact_messages_with_claude(
                        messages, ctx.model_id, session_id=session_id, model_key=ctx.model_key
                    )
                    messages = sanitize_tool_pairs(messages)
                    continue  # retry the round
                if ctx.policy.salvage_round and not salvage_mode:
                    # Salvage: one final no-tools round on a hard-trimmed tail,
                    # so the work already done yields an answer, not an error.
                    salvage_mode = True
                    log.warning("Reactive compaction exhausted — salvage round")
                    yield f"data: {ndjson_dumps({'status': 'Context exceeds the model window; synthesizing a final answer from what fits...'})}\n\n"
                    tail = sanitize_tool_pairs(ensure_valid_start(messages[-6:]))
                    tail.append(
                        {
                            "role": "user",
                            "content": (
                                "[The conversation no longer fits the model's context "
                                "window and was truncated. Synthesize the best possible "
                                "final answer from the work above. Do not call tools.]"
                            ),
                        }
                    )
                    messages = tail
                    continue
                yield f"data: {ndjson_dumps({'error': 'Conversation too long even after compaction. Please start a new session.'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            except WhisperAPIError as api_err:
                yield f"data: {ndjson_dumps({'error': api_err.user_message})}\n\n"
                yield "data: [DONE]\n\n"
                return

            if round_result is None:
                log.warning("Adapter round ended without a result — ending stream")
                yield "data: [DONE]\n\n"
                return

            stop_reason = round_result.stop_reason
            usage = round_result.usage
            result_content = round_result.content

            # ── Usage frame + cost accounting ────────────────────────────────
            round_input_tokens = usage.input_tokens
            round_output_tokens = usage.output_tokens
            round_cache_read = usage.cache_read_tokens
            round_cache_creation = usage.cache_write_tokens
            total_input_tokens += round_input_tokens
            total_output_tokens += round_output_tokens
            total_cache_read += round_cache_read
            total_cache_creation += round_cache_creation
            _cached_in_input = getattr(ctx.adapter, "cached_in_input", False)
            cost = _estimate_cost(
                ctx.model_key,
                total_input_tokens,
                total_output_tokens,
                total_cache_read,
                total_cache_creation,
                cached_in_input=_cached_in_input,
            )
            from server.chat.loop_hints import context_window_for, note_prompt_tokens

            _ctx_max = context_window_for(ctx.model_key)
            _prompt_tokens = prompt_token_total(
                round_input_tokens,
                round_cache_read,
                round_cache_creation,
                cached_in_input=_cached_in_input,
            )
            total_prompt_tokens += _prompt_tokens
            note_prompt_tokens(_scope_id, _prompt_tokens, _ctx_max)
            yield f"data: {ndjson_dumps({'usage': {'input_tokens': round_input_tokens, 'output_tokens': round_output_tokens, 'total_input': total_input_tokens, 'total_output': total_output_tokens, 'total_prompt': total_prompt_tokens, 'cache_read_tokens': round_cache_read, 'cache_creation_tokens': round_cache_creation, 'total_cache_read': total_cache_read, 'total_cache_creation': total_cache_creation, 'estimated_cost_usd': round(cost, 6), 'model': ctx.model_key, 'context_used': _prompt_tokens, 'context_max': _ctx_max}})}\n\n"
            _record_cost_turn(
                session_id=session_id,
                turn_number=round_num,
                model=ctx.model_key,
                input_tokens=round_input_tokens,
                output_tokens=round_output_tokens,
                cache_read_tokens=round_cache_read,
                cache_creation_tokens=round_cache_creation,
                cost_usd=_estimate_cost(
                    ctx.model_key,
                    round_input_tokens,
                    round_output_tokens,
                    round_cache_read,
                    round_cache_creation,
                    cached_in_input=_cached_in_input,
                ),
            )

            # ── Tool-use extraction with replay protection ───────────────────
            tool_uses_raw = [b for b in result_content if b["type"] == "tool_use"]
            tool_uses = []
            for _tu in tool_uses_raw:
                if not _seen_tool_ids.has(_tu["id"]):
                    _seen_tool_ids.add(_tu["id"])
                    tool_uses.append(_tu)

            if stop_reason == "max_tokens":
                # Cut off mid-answer: strip partial tool_use and continue.
                # The continuation is a fresh provider request (full prompt
                # reprocessing, possible throttle backoff), so tell the UI why
                # the text stopped mid-sentence instead of leaving a silent
                # stall that reads as a hang.
                yield f"data: {ndjson_dumps({'status': 'Output limit reached; continuing the response...'})}\n\n"
                messages.append(
                    {"role": "assistant", "content": strip_partial_tool_use(result_content)}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Continue exactly where you left off. Do not repeat anything. "
                            "IMPORTANT: If you were in the middle of a code block (```html or similar), "
                            "continue the code directly — do NOT close and reopen the fence, do NOT add explanation text "
                            "before or inside the code. Just continue the code from the exact point it was cut off. "
                            "The output will be concatenated to your previous response."
                        ),
                    }
                )
                if estimate_message_size(messages) > thresholds_for(ctx.model_key)[0]:
                    messages = await compact_messages_with_claude(
                        messages, ctx.model_id, session_id=session_id, model_key=ctx.model_key
                    )
                    messages = ensure_attachments_present(messages, session_id)
                continue

            if stop_reason == "pause_turn" and result_content:
                # Anthropic paused a long-running turn: adaptive-thinking or
                # long tool streams can run past the stream window, and the API
                # returns pause_turn to hand the turn back mid-work. The turn is
                # NOT complete. Resubmit the accumulated (well-formed, resumable)
                # assistant content so the model continues from where it paused.
                # Treating pause_turn as terminal is exactly the "chat stops for
                # no reason mid-task, no error" bug. Bounded by the round cap
                # like any other round (a pause storm burns rounds, never loops
                # forever); each pause is logged so a stall is visible.
                log.info(
                    "stop_reason=pause_turn (round %d) — resubmitting to continue the turn",
                    round_num,
                )
                yield f"data: {ndjson_dumps({'status': 'Model paused mid-turn; resuming...'})}\n\n"
                messages.append({"role": "assistant", "content": result_content})
                continue

            # Everything left here is a genuine end-of-turn (end_turn,
            # stop_sequence, refusal) or an unknown stop reason we conservatively
            # treat as terminal rather than risk an unbounded resubmit loop.
            if stop_reason != "tool_use" or not tool_uses:
                # Completion gate (WS-E): Stop hooks + goal evaluator. A block
                # injects the feedback and loops again, bounded by the cap.
                if (
                    not is_last_round
                    and stop_blocks_used < _goal_cap
                    and ctx.policy.completion_gate
                ):
                    from server.goals import GateContext
                    from server.goals.gate import run_completion_gate

                    _gate = await run_completion_gate(
                        GateContext(
                            session_id=_scope_id,
                            messages=messages,
                            goal=goal_text,
                            provider=ctx.adapter.provider,
                            model_id=ctx.model_id,
                            workspace=ctx.ws_path,
                            attempt=stop_blocks_used,
                            max_consecutive_blocks=_goal_cap,
                        )
                    )
                    if _gate.frame:
                        yield f"data: {ndjson_dumps(_gate.frame)}\n\n"
                    if _gate.block:
                        stop_blocks_used += 1
                        _assistant = strip_partial_tool_use(result_content)
                        _has_text = isinstance(_assistant, list) and any(
                            isinstance(b, dict)
                            and b.get("type") == "text"
                            and (b.get("text") or "").strip()
                            for b in _assistant
                        )
                        if not _has_text:
                            _assistant = [{"type": "text", "text": "(continuing)"}]
                        messages.append({"role": "assistant", "content": _assistant})
                        messages.append(
                            {
                                "role": "user",
                                "content": f"[completion gate] {_gate.feedback}",
                            }
                        )
                        continue

                log.info(
                    "Stream ending: stop_reason=%s, tool_uses=%d, round=%d",
                    stop_reason,
                    len(tool_uses),
                    round_num,
                )

                # Post-turn memory hooks (fire-and-forget background tasks).
                if ctx.memory_hooks is not None:
                    ctx.memory_hooks(messages)
                else:
                    _fire_memory_hooks(messages, session_id, ctx.ws_path, ctx.model_id)

                yield "data: [DONE]\n\n"
                return

            messages.append({"role": "assistant", "content": result_content})

            # ── Tool execution through the shared safety gate ────────────────
            from server.agents.event_bus import event_bus as _agent_event_bus

            _event_queue = _agent_event_bus.subscribe(session_id)
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
                    model_id=ctx.model_id
                    if ctx.tool_exec_model_id is None
                    else ctx.tool_exec_model_id,
                    plan_mode=ctx.plan_mode,
                    mode=ctx.mode,
                    # Subagents inherit the turn's clamped effort so an
                    # ultracode parent no longer fans out to plain children.
                    effort_label=ctx.effort_label,
                    unattended=ctx.unattended,
                )
            )

            def _route_event(ev: dict) -> dict | None:
                """Agent-runtime events → team_progress frames; typed events
                delivered by the long-lived session stream are skipped to
                prevent double delivery."""
                if ev.get("type") in (
                    "cron_event",
                    "memory_event",
                    "task_event",
                    "cron_progress",
                    "ci_progress",
                    "ci_result",
                    "session_message",
                    # A workflow's own events reach the UI two other ways: the
                    # run card's per-run EventSource for live progress, and a
                    # task_event (above) for the terminal outcome. Without this
                    # they were relabelled as team_progress mid-batch, which
                    # renders a detached workflow as if it were this turn's
                    # subagent team.
                    "workflow_event",
                ):
                    return None
                return {"team_progress": ev}

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
                _agent_event_bus.unsubscribe(session_id, _event_queue)

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
                # Keys the auto-mode circuit breaker — turn-scoped state, same
                # class as the goal store above.
                session_id=_scope_id,
                unattended=ctx.unattended,
            )

            for evt in sse_events:
                yield f"data: {evt}\n\n"
            for trunc_evt in truncation_events:
                yield f"data: {ndjson_dumps(trunc_evt)}\n\n"

            if has_user_question or has_pending_approval:
                # Stash the paused conversation so the continuation turn can
                # rebuild a well-formed request with placeholders replaced.
                paused_sessions[_scope_id] = {
                    "messages": list(messages),
                    "pending_tool_results": list(tool_results),
                    "provider": ctx.adapter.provider,
                }
                yield "data: [DONE]\n\n"
                return

            log.info("Round %d: %d tool results, continuing loop", round_num, len(tool_results))

            if not tool_results:
                log.warning("No tool results to send back — ending stream")
                yield "data: [DONE]\n\n"
                return

            messages.append({"role": "user", "content": tool_results})

            # Proactive compaction — char estimate, supplemented by TOKEN
            # truth (the per-round usage crossed 80% of the window).
            from server.chat.loop_hints import should_nudge_compaction

            if estimate_message_size(messages) > thresholds_for(ctx.model_key)[
                0
            ] or should_nudge_compaction(_scope_id):
                # An LLM summarization call sits between rounds here; without a
                # frame the wait reads as a mid-turn hang.
                yield f"data: {ndjson_dumps({'status': 'Compacting conversation context...'})}\n\n"
                messages = await compact_messages_with_claude(
                    messages, ctx.model_id, session_id=session_id, model_key=ctx.model_key
                )
                # Restore any attachment the summary swallowed. The REACTIVE
                # path above deliberately does not re-inject (it must shrink;
                # analyze_document has session-wide access as the fallback).
                messages = ensure_attachments_present(messages, session_id)
        finally:
            # Torn down mid-round (Stop / tab-close) or round finished — cancel
            # a still-running tool batch. The adapter's own finally releases
            # the provider stream.
            if _batch_task is not None and not _batch_task.done():
                _batch_task.cancel()

    yield f"data: {ndjson_dumps({'text': '(Reached maximum tool rounds)'})}\n\n"
    yield "data: [DONE]\n\n"


def _fire_memory_hooks(messages, session_id, ws_path, model_id) -> None:
    """Post-turn fire-and-forget hooks: auto-memory extraction, session
    memory, dream consolidation. Flag-gated; failures never touch the turn."""
    from server.infrastructure.async_tasks import spawn
    from server.infrastructure.feature_flags import is_enabled

    if is_enabled("auto_memory"):
        from server.memory.extract import maybe_extract_memory

        spawn(
            maybe_extract_memory(
                messages=messages,
                session_id=session_id,
                ws_path=ws_path,
                model_id=model_id,
            ),
            name="auto-memory-extract",
        )
    if is_enabled("session_memory"):
        from server.memory.session_memory import maybe_update_session_memory

        spawn(
            maybe_update_session_memory(
                messages=messages,
                session_id=session_id,
                model_id=model_id,
            ),
            name="session-memory-update",
        )
    if is_enabled("dream_consolidation"):
        from server.memory.dream import record_and_maybe_dream

        spawn(record_and_maybe_dream(ws_path, model_id=model_id), name="dream-consolidation")
