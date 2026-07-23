"""SSE adapter for OpenAI-on-Bedrock (GPT-5.5 / GPT-5.4) chat turns.

Emits the SAME event contract the frontend consumes for every other provider
(``text`` / ``thinking_*`` / ``skill`` / ``skill_input`` / ``skill_result`` /
``approval_request`` / ``usage`` / ``[DONE]``) by streaming the OpenAI Responses
API and mapping its event types. Tool calls reuse the shared executor +
approval pipeline (``server.local.tools.run_tool_round``), exactly like the
on-device path, so skills, MCP tools, and the destructive-action approval gate
behave identically. Reasoning summaries become the ``thinking_*`` channel.
"""

from __future__ import annotations

import asyncio
import json
import logging

from server.local.tools import run_tool_round
from server.openai_bedrock import runtime as oai
from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")

_MAX_TOOL_ROUNDS = 50
# bedrock-mantle holds the stream open ~30-40s after the answer is fully
# generated before sending response.completed (observed; reasoning-independent).
# We poll the event queue frequently, emit an SSE keepalive every _HEARTBEAT_S
# of idle so the browser never drops the connection, and EARLY-RELEASE the round
# once output is complete and the stream has been idle for _EARLY_RELEASE_GRACE_S
# rather than waiting out the tail.
_POLL_S = 1.0
_HEARTBEAT_S = 8.0
_EARLY_RELEASE_GRACE_S = 2.0

# Turns paused awaiting an approval continuation, keyed by session_id. In-process
# + ephemeral, exactly like the cloud ``_paused_sessions`` and the on-device
# ``_local_paused`` — a restart drops it and a continuation that finds nothing is
# handled as a fresh turn by the router (no blind re-execution).
_openai_paused: dict[str, dict] = {}


def has_openai_pause(session_id: str) -> bool:
    return session_id in _openai_paused


def _frame(obj) -> str:
    return f"data: {ndjson_dumps(obj)}\n\n"


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if any(s in low for s in ("auth", "401", "403", "denied", "credential", "token")):
        return (
            "GPT-5.x auth/access failed. Enable OpenAI model access in the "
            "Bedrock console for this region, confirm your AWS credentials are "
            f"valid, and that the model runs in the configured region. ({msg[:200]})"
        )
    if any(s in low for s in ("not found", "404", "does not exist", "no such model")):
        return (
            "GPT-5.x not found in this region. GPT-5.5 is served only in "
            "us-east-1 / us-east-2 (GPT-5.4 adds us-west-2). Point bedrock_region "
            "at a supported region, or set the model's openai_region override to "
            f"one. ({msg[:200]})"
        )
    return f"OpenAI (Bedrock) request failed: {msg[:240]}"


def _usage_frame(model_key: str, input_tokens: int, output_tokens: int, cached: int = 0) -> str:
    try:
        from server.costs.tracker import estimate_cost

        # OpenAI input_tokens already includes cached input — bill the cached
        # portion once at the cache_read rate (cached_in_input=True).
        cost = estimate_cost(
            model_key, input_tokens, output_tokens, cache_read_tokens=cached, cached_in_input=True
        )
    except Exception:
        cost = 0.0
    return _frame(
        {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_input": input_tokens,
                "total_output": output_tokens,
                "cache_read_tokens": cached,
                "total_cache_read": cached,
                "estimated_cost_usd": cost,
                "model": model_key,
            }
        }
    )


def _record_cost(
    model_key: str,
    session_id: str,
    round_num: int,
    input_tokens: int,
    output_tokens: int,
    cached: int = 0,
) -> None:
    try:
        from server.costs.tracker import estimate_cost, record_turn

        record_turn(
            session_id=session_id,
            turn_number=round_num,
            model=model_key,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached,
            cache_creation_tokens=0,
            cost_usd=estimate_cost(
                model_key,
                input_tokens,
                output_tokens,
                cache_read_tokens=cached,
                cached_in_input=True,
            ),
        )
    except Exception as e:
        log.debug("openai cost record failed: %s", e)


async def _stream_round(stream, state: dict):
    """Consume ONE Responses stream, yielding SSE frames as content decodes, and
    fill ``state`` with the round's outcome (``fcalls``, token usage, ``errored``).

    Two adaptations for bedrock-mantle, which holds the stream open ~30-40s after
    the answer is fully generated before sending ``response.completed`` (observed
    live; independent of reasoning effort):

    * EARLY RELEASE — once output is functionally complete (``output_text.done``
      plus every started function_call's args ``.done``) and the stream goes
      briefly idle, stop waiting for ``response.completed`` and move on. This
      collapses a ~43s turn to a few seconds and removes the per-round tail in
      tool loops. Token usage lives only in the laggy ``completed`` event, so it
      is then best-effort estimated.
    * HEARTBEAT — during any idle gap, emit an SSE comment so the browser never
      drops the connection (the cause of the ERR_NETWORK_IO_SUSPENDED / "network
      error" failures); the chat stream previously had no keepalive.
    """
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    async def produce():
        try:
            async for ev in stream:
                q.put_nowait(("ev", ev))
        except Exception as e:  # noqa: BLE001 — surfaced to the consumer
            q.put_nowait(("exc", e))
        finally:
            q.put_nowait(("end", None))

    prod = asyncio.ensure_future(produce())

    fcalls: dict[str, dict] = {}
    n_items = 0  # function_call items started
    n_done = 0  # function_call arg streams completed
    text_done = False
    out_chars = 0
    in_tok = out_tok = cached_tok = 0
    completed = False
    thinking_open = False
    last_event = loop.time()
    last_hb = loop.time()

    try:
        while True:
            ready = text_done or (n_items > 0 and n_done >= n_items)
            try:
                kind, payload = await asyncio.wait_for(q.get(), timeout=_POLL_S)
            except asyncio.TimeoutError:
                now = loop.time()
                # Answer already complete -> skip mantle's completed-tail.
                if ready and (now - last_event) > _EARLY_RELEASE_GRACE_S:
                    break
                if (now - last_hb) >= _HEARTBEAT_S:
                    last_hb = now
                    yield ": hb\n\n"
                continue
            last_hb = loop.time()
            if kind == "end":
                break
            if kind == "exc":
                state["errored"] = True
                yield _frame({"error": _friendly_error(payload)})
                break

            ev = payload
            et = getattr(ev, "type", "") or ""
            if et == "response.output_text.delta":
                d = getattr(ev, "delta", "") or ""
                if thinking_open:
                    thinking_open = False
                    yield _frame({"thinking_stop": True})
                out_chars += len(d)
                last_event = loop.time()
                yield _frame({"text": d})
            elif et == "response.output_text.done":
                text_done = True
                last_event = loop.time()
            elif et in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                d = getattr(ev, "delta", "") or ""
                if d:
                    if not thinking_open:
                        thinking_open = True
                        yield _frame({"thinking_start": True})
                    last_event = loop.time()
                    yield _frame({"thinking": d})
            elif et == "response.refusal.delta":
                d = getattr(ev, "delta", "") or ""
                out_chars += len(d)
                last_event = loop.time()
                yield _frame({"text": d})
            elif et == "response.output_item.added":
                item = getattr(ev, "item", None)
                if item is not None and getattr(item, "type", "") == "function_call":
                    n_items += 1
                    fcalls[getattr(item, "id", "") or ""] = {
                        "call_id": getattr(item, "call_id", "") or "",
                        "name": getattr(item, "name", "") or "",
                        "args": "",
                    }
                    last_event = loop.time()
            elif et == "response.function_call_arguments.delta":
                fc = fcalls.get(getattr(ev, "item_id", "") or "")
                if fc is not None:
                    fc["args"] += getattr(ev, "delta", "") or ""
                    last_event = loop.time()
            elif et == "response.function_call_arguments.done":
                fc = fcalls.get(getattr(ev, "item_id", "") or "")
                if fc is not None:
                    da = getattr(ev, "arguments", None)
                    if da:
                        fc["args"] = da
                n_done += 1
                last_event = loop.time()
            elif et == "response.completed":
                completed = True
                resp = getattr(ev, "response", None)
                usage = getattr(resp, "usage", None)
                in_tok = int(getattr(usage, "input_tokens", 0) or 0)
                out_tok = int(getattr(usage, "output_tokens", 0) or 0)
                itd = getattr(usage, "input_tokens_details", None)
                cached_tok = int(getattr(itd, "cached_tokens", 0) or 0)
                break
            elif et in ("response.failed", "error"):
                resp = getattr(ev, "response", None)
                err = getattr(resp, "error", None) or getattr(ev, "message", "") or "stream error"
                state["errored"] = True
                yield _frame({"error": str(getattr(err, "message", err))})
                break
            elif et == "response.incomplete":
                yield _frame({"text": "\n\n*(Response truncated: output token limit reached.)*"})

        if thinking_open:
            yield _frame({"thinking_stop": True})
    finally:
        prod.cancel()
        try:
            await stream.close()
        except Exception:  # noqa: BLE001 — best-effort; we're abandoning the tail
            pass

    state["fcalls"] = list(fcalls.values())
    state["cached_tokens"] = cached_tok
    state["exact_usage"] = completed
    if completed:
        state["input_tokens"] = in_tok
        state["output_tokens"] = out_tok
    else:
        # Released before mantle's completed-tail: estimate output tokens from
        # streamed chars (~4 chars/token); input is unknown without the usage
        # event (cost pricing for these models is a placeholder anyway).
        state["input_tokens"] = 0
        state["output_tokens"] = max(1, out_chars // 4)


def _assemble_tools(tool_ctx: dict) -> list[dict]:
    from server.chat.tool_pool import assemble_tool_pool

    # Progressive disclosure pays most here in real dollars: the Responses
    # API has no explicit cache checkpoints, so every deferred schema is a
    # token the request simply never carries.
    pool = assemble_tool_pool(
        plan_mode=tool_ctx.get("plan_mode", False),
        ws_connected=tool_ctx.get("ws_connected", False),
        mcp_enabled_names=tool_ctx.get("mcp_enabled_names"),
        suppress_workspace_search=tool_ctx.get("suppress_ws_search", False),
        session_id=tool_ctx.get("session_id", ""),
        progressive=True,
    )
    # Drop tools that GPT-5.5's literal prompt-following turns into foot-guns
    # (e.g. it answers "think for 10 seconds" by actually calling `sleep`).
    pool = [t for t in pool if t.get("name") not in oai.EXCLUDED_TOOLS]
    return oai.translate_tools(pool)


async def _tool_loop(
    model_key,
    model_id,
    instructions,
    input_items,
    tool_ctx,
    *,
    session_id,
    effort,
    verbosity,
    start_round=0,
    memory_ctx=None,
):
    """The agentic loop, shared by fresh turns and approval resumes. Streams one
    Responses round, runs any tool calls through the shared pipeline, feeds the
    outputs back as function_call_output items, and repeats until the model
    answers without calling a tool. On a pending approval it stashes state and
    ends the stream; the resume picks up from the next round."""
    tools = _assemble_tools(tool_ctx) if tool_ctx else None
    region = oai.region_for(model_key)
    total_in = total_out = total_cached = 0
    from server.chat.tool_activation import version as _activation_version

    _tools_version = _activation_version(session_id)
    # Completion-gate parity with the cloud path: Stop hooks + goal evaluator
    # can force the GPT loop to keep going toward the goal. Capped per turn.
    _ws_path = (memory_ctx or {}).get("ws_path")
    stop_blocks_used = 0
    from server.goals import DEFAULT_MAX_CONSECUTIVE_BLOCKS
    from server.goals import store as _goal_store

    _goal_cap = DEFAULT_MAX_CONSECUTIVE_BLOCKS
    try:
        from server.infrastructure import config as _cfg

        _goal_cap = int(_cfg.get("goal_max_consecutive_blocks", _goal_cap))
    except Exception:
        pass
    _goal_text = _goal_store.get_goal(session_id)["goal"] if session_id else ""
    if start_round == 0 and _goal_text:
        _goal_store.reset_for_new_turn(session_id)

    for rnd in range(start_round, _MAX_TOOL_ROUNDS):
        # A tool_search in the previous round activated new tools: reassemble
        # so they're callable. Otherwise keep the same list (stable Responses
        # prompt-cache key).
        if tool_ctx and _activation_version(session_id) != _tools_version:
            _tools_version = _activation_version(session_id)
            tools = _assemble_tools(tool_ctx)
        # Final-round parity with the cloud path (routes.py sends no tools on
        # its last round): forbid tool calls so the model must synthesize an
        # answer from what it has instead of dying at the cap with nothing.
        # Tools stay in the request (tool_choice="none") so the prior
        # function_call history still validates and the cached prefix holds.
        is_last_round = rnd == _MAX_TOOL_ROUNDS - 1
        client = oai.build_client(region)
        try:
            stream = await client.responses.create(
                model=model_id,
                instructions=instructions,
                input=input_items,
                reasoning={"effort": effort, "summary": "auto"},
                text={"verbosity": verbosity},
                stream=True,
                store=False,
                # Prompt caching: a stable per-session key lets the endpoint
                # reuse the cached system+tools prefix across this session's
                # turns (cheaper input, lower latency). Independent of `store`.
                prompt_cache_key=f"ws-{session_id}",
                **(
                    {"tools": tools, "tool_choice": "none" if is_last_round else "auto"}
                    if tools
                    else {}
                ),
            )
        except Exception as e:  # noqa: BLE001 — surface any client/setup error
            log.warning("OpenAI responses.create failed (%s): %s", model_key, e)
            yield _frame({"error": _friendly_error(e)})
            yield "data: [DONE]\n\n"
            return

        round_state: dict = {}
        try:
            async for frame in _stream_round(stream, round_state):
                yield frame
        except Exception as e:  # noqa: BLE001 — mid-stream transport error
            log.warning("OpenAI stream error (%s): %s", model_key, e)
            yield _frame({"error": _friendly_error(e)})
            round_state["errored"] = True

        round_in = int(round_state.get("input_tokens", 0))
        round_out = int(round_state.get("output_tokens", 0))
        round_cached = int(round_state.get("cached_tokens", 0))
        fcalls = round_state.get("fcalls", [])

        total_in += round_in
        total_out += round_out
        total_cached += round_cached
        if round_state.get("errored"):
            yield "data: [DONE]\n\n"
            return

        _record_cost(model_key, session_id, rnd, round_in, round_out, round_cached)

        valid = [f for f in fcalls if f.get("name")]
        if not valid:
            # Completion gate — Stop hooks + goal evaluator; a block injects the
            # feedback and loops again (bounded by the consecutive-block cap).
            if not is_last_round and stop_blocks_used < _goal_cap:
                from server.goals import GateContext
                from server.goals.gate import run_completion_gate

                _gate = await run_completion_gate(
                    GateContext(
                        session_id=session_id,
                        messages=input_items,
                        goal=_goal_text,
                        provider="openai",
                        model_id=model_id,
                        workspace=_ws_path,
                        attempt=stop_blocks_used,
                        max_consecutive_blocks=_goal_cap,
                    )
                )
                if _gate.frame:
                    yield _frame(_gate.frame)
                if _gate.block:
                    stop_blocks_used += 1
                    input_items.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"[completion gate] {_gate.feedback}",
                                }
                            ],
                        }
                    )
                    continue
            yield _usage_frame(model_key, total_in, total_out, total_cached)
            yield "data: [DONE]\n\n"
            return

        # Append the model's function_call items so the follow-up request has
        # matching call_ids, and surface the calls to the UI.
        tool_uses = []
        for f in valid:
            try:
                args = json.loads(f["args"]) if f["args"].strip() else {}
            except Exception:
                args = {}
            input_items.append(
                {
                    "type": "function_call",
                    "call_id": f["call_id"],
                    "name": f["name"],
                    "arguments": f["args"] or "{}",
                }
            )
            tool_uses.append({"id": f["call_id"], "name": f["name"], "input": args})
            yield _frame({"skill": f["name"]})
            yield _frame({"skill_input": f["name"], "input": args})

        tool_results, sse_events, has_pending_approval, has_user_question = await run_tool_round(
            tool_uses,
            session_id=session_id,
            plan_mode=tool_ctx.get("plan_mode", False),
            mode=tool_ctx.get("mode", "default"),
            session_approvals=tool_ctx.get("session_approvals"),
            session_denials=tool_ctx.get("session_denials"),
            config=tool_ctx.get("config"),
            transcript=tool_ctx.get("transcript", ""),
            model_id=model_id,
        )
        for ev in sse_events:
            yield f"data: {ev}\n\n"

        if has_pending_approval or has_user_question:
            # Hard stop: no destructive action ran (the handler returned a
            # sentinel). Stash so the approval continuation can fill the result
            # and resume from the next round.
            _openai_paused[session_id] = {
                "model_key": model_key,
                "model_id": model_id,
                "instructions": instructions,
                "input_items": input_items,
                "tool_ctx": tool_ctx,
                "pending_results": tool_results,
                "names_by_id": {tu["id"]: tu["name"] for tu in tool_uses},
                "effort": effort,
                "verbosity": verbosity,
                "next_round": rnd + 1,
                # Carried so the RESUME (not this pause) fires the post-turn
                # memory hooks at the true end of the turn.
                "memory_ctx": memory_ctx,
            }
            yield "data: [DONE]\n\n"
            return

        for r in tool_results:
            input_items.extend(
                oai.tool_result_input_items(r.get("tool_use_id", ""), r.get("content", ""))
            )

    yield _frame({"text": "\n\n*(Reached the tool-call round limit without a final answer.)*"})
    yield _usage_frame(model_key, total_in, total_out, total_cached)
    yield "data: [DONE]\n\n"


def _spawn_memory_hooks(messages, session_id, ws_path, model_id) -> None:
    """Fire the post-turn memory hooks the cloud path runs (routes.py), so a
    GPT-5.x turn also feeds auto-memory, session memory, and dream consolidation
    instead of writing nothing. ``model_id`` is safe to pass: the extraction
    agent ignores it (uses the memory_extractor model) and session memory only
    uses it for the is-local check (False for an OpenAI model id)."""
    from server.infrastructure.async_tasks import spawn
    from server.infrastructure.feature_flags import is_enabled

    # Parity with routes.py: extraction runs without a workspace too (routes
    # to the global tier only).
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

        spawn(
            record_and_maybe_dream(ws_path, model_id=model_id),
            name="dream-consolidation",
        )


async def stream_openai_chat(
    *,
    model_key,
    model_id,
    system_prompt,
    messages,
    session_id,
    effort,
    verbosity,
    tool_ctx,
    ws_path=None,
):
    input_items = oai.to_responses_input(messages)
    # GPT-5.5 follows instructions literally; nudge it away from satisfying
    # timing phrases by actually waiting. (Carried through pauses via the
    # stashed instructions, so resumes inherit it too.)
    instructions = system_prompt + oai.GPT_INSTRUCTIONS_SUFFIX
    try:
        async for chunk in _tool_loop(
            model_key,
            model_id,
            instructions,
            input_items,
            tool_ctx,
            session_id=session_id,
            effort=effort,
            verbosity=verbosity,
            memory_ctx={"messages": messages, "ws_path": ws_path, "model_id": model_id},
        ):
            yield chunk
        # Feed memory the same way the Bedrock path does — but ONLY if the turn
        # actually finished. If it paused for an approval, the resume fires the
        # hooks at the true end (firing here would run on a half-finished turn
        # and never on the resumed remainder). Fire-and-forget; flags +
        # workspace + throttles gate it inside.
        if not has_openai_pause(session_id):
            _spawn_memory_hooks(messages, session_id, ws_path, model_id)
    except Exception as e:  # noqa: BLE001 — last-resort guard so SSE always closes
        log.warning("OpenAI chat (%s) failed: %s", model_key, e)
        yield _frame({"error": _friendly_error(e)})
        yield "data: [DONE]\n\n"


async def resume_openai_chat(session_id, approved_tool_result, session_approvals=None):
    """Resume a paused OpenAI tool turn after the user approved/denied an action.
    The action already ran server-side via /api/approval/execute; here we inject
    its result as a function_call_output and continue the loop. Falls back to a
    clean [DONE] when no paused state exists (e.g. after a restart)."""
    paused = _openai_paused.pop(session_id, None)
    if not paused:
        log.warning("resume_openai_chat: no paused state for %s — ending.", session_id)
        yield "data: [DONE]\n\n"
        return

    answers = (
        approved_tool_result if isinstance(approved_tool_result, list) else [approved_tool_result]
    )
    answers = [a for a in answers if isinstance(a, dict)]
    by_id = {a.get("tool_use_id", ""): a for a in answers}

    tool_results = paused["pending_results"]
    names_by_id = paused.get("names_by_id", {})
    for r in tool_results:
        ans = by_id.get(r.get("tool_use_id", ""))
        if ans is not None:
            r["content"] = ans.get("content", "")
            name = names_by_id.get(r.get("tool_use_id", ""), "tool")
            yield _frame({"skill_result": name, "output": str(r["content"])[:2000]})

    input_items = paused["input_items"]
    for r in tool_results:
        input_items.extend(
            oai.tool_result_input_items(r.get("tool_use_id", ""), r.get("content", ""))
        )

    tool_ctx = paused["tool_ctx"]
    if session_approvals is not None:
        tool_ctx = {**tool_ctx, "session_approvals": session_approvals}

    mem = paused.get("memory_ctx")
    async for chunk in _tool_loop(
        paused["model_key"],
        paused["model_id"],
        paused["instructions"],
        input_items,
        tool_ctx,
        session_id=session_id,
        effort=paused["effort"],
        verbosity=paused["verbosity"],
        start_round=paused["next_round"],
        memory_ctx=mem,
    ):
        yield chunk
    # The turn is truly done now (unless it paused AGAIN for another approval,
    # in which case the next resume will fire the hooks). Feed memory here, the
    # deferred counterpart of the pass in stream_openai_chat.
    if mem and not has_openai_pause(session_id):
        _spawn_memory_hooks(mem["messages"], session_id, mem["ws_path"], mem["model_id"])
