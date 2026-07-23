"""SSE adapter for on-device chat turns.

Emits the SAME event contract the frontend already consumes for Bedrock
(``text`` / ``thinking_*`` / ``skill`` / ``skill_input`` / ``skill_result`` /
``approval_request`` / ``usage`` / ``[DONE]``) so the chat UI is agnostic to
whether a turn ran on Claude or a local GGUF. Local turns are $0 and never call
AWS for generation.

Two paths:

* **plain / thinking** (tools off) — the blocking llama.cpp generator
  (``runtime.iter_chat``) is thread-affine, so we pump its chunks onto an
  asyncio.Queue and yield them out. Text is token-streamed; the optional thought
  channel becomes ``thinking_*`` events.

* **tools on** — an async agentic loop with FULL tool parity. Each round
  generates on the model thread (``run_in_executor``), parses Gemma's tool-call
  DSL, and runs the calls through the EXISTING executor + approval pipeline
  (``server/local/tools.run_tool_round`` → ``execute_tool_batch`` +
  ``process_tool_results``). Read-only / pre-approved tools loop until the model
  answers; a destructive tool that needs approval pauses the turn (we forward the
  real ``approval_request`` card, stash the Gemma conversation, and emit
  ``[DONE]``), then resumes on the approval continuation. The safety gate lives
  entirely in ``process_tool_results`` — see server/local/tools.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from server.local import runtime as local_llm
from server.local.tools import (
    gemma_call_to_tool_use,
    get_tool_schemas,
    parse_tool_calls,
    run_tool_round,
)
from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")

_MAX_TOOL_ROUNDS = 24

# Local tool turns paused awaiting an approval continuation, keyed by session_id.
# In-process + ephemeral, exactly like the cloud ``_paused_sessions`` — a server
# restart drops it, and a continuation that finds no entry is handled as a fresh
# turn by the router (no blind re-execution).
_local_paused: dict[str, dict] = {}


def has_local_pause(session_id: str) -> bool:
    return session_id in _local_paused


def _spawn_session_update(model_key: str, messages: list[dict], session_id: str) -> None:
    """Fire-and-forget on-device session-memory update after a local turn.

    The cadence + feature-flag gate lives in ``maybe_update_session_memory``;
    this just kicks it off (on the model's executor thread, via the local
    summariser) so session memory auto-builds fully offline. It runs one turn
    behind — the just-generated answer is captured on the next turn via the
    persisted history — which avoids threading the answer text out of the
    stream and keeps the model thread free while the response is still flushing.
    """
    if not messages:
        return
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("session_memory"):
            return
        from server.infrastructure.async_tasks import spawn
        from server.local.runtime import local_model_meta
        from server.memory.session_memory import maybe_update_session_memory

        model_id = local_model_meta(model_key).get("id", "")
        spawn(
            maybe_update_session_memory(
                messages=list(messages),
                session_id=session_id,
                model_id=model_id,
            ),
            name="local-session-memory-update",
        )
    except Exception as e:  # never let memory bookkeeping disrupt a turn
        log.debug("Could not spawn local session update: %s", e)


def _spawn_extraction(
    model_key: str, messages: list[dict], session_id: str, ws_path: str | None
) -> None:
    """Fire-and-forget auto-memory extraction after a local turn.

    The extraction agent runs through the cloud agent runtime (run_agent
    resolves the memory_extractor model itself; the local model id passed here
    is ignored), so it only fires when cloud access is allowed: in "local"
    model mode the app is fully offline and extraction skips gracefully, while
    hybrid/cloud modes get the same two-tier extraction the cloud paths spawn
    (see chat/routes.py and openai_bedrock/stream.py). Throttle + cursor +
    feature-flag gates all live inside ``maybe_extract_memory``.
    """
    if not messages:
        return
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("auto_memory"):
            return
        from server.infrastructure.model_mode import current_mode

        if current_mode() == "local":
            log.debug("Skipping auto-memory extraction: fully offline (model_mode=local)")
            return
        from server.infrastructure.async_tasks import spawn
        from server.local.runtime import local_model_meta
        from server.memory.extract import maybe_extract_memory

        model_id = local_model_meta(model_key).get("id", "")
        spawn(
            maybe_extract_memory(
                messages=list(messages),
                session_id=session_id,
                ws_path=ws_path,
                model_id=model_id,
            ),
            name="local-auto-memory-extract",
        )
    except Exception as e:  # never let memory bookkeeping disrupt a turn
        log.debug("Could not spawn local memory extraction: %s", e)


def _spawn_dream(model_key: str, ws_path: str | None) -> None:
    """Fire-and-forget dream consolidation after a local turn.

    Mirrors the cloud paths (chat/routes.py and openai_bedrock/stream.py):
    record the session against each memory tier and consolidate any tier that
    is due. The consolidator agent runs on a cloud model, so — like
    ``_spawn_extraction`` above — this skips gracefully when the app is fully
    offline (model_mode=local). Cadence + feature-flag gates also live inside
    ``record_and_maybe_dream``.
    """
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("dream_consolidation"):
            return
        from server.infrastructure.model_mode import current_mode

        if current_mode() == "local":
            log.debug("Skipping dream consolidation: fully offline (model_mode=local)")
            return
        from server.infrastructure.async_tasks import spawn
        from server.local.runtime import local_model_meta
        from server.memory.dream import record_and_maybe_dream

        model_id = local_model_meta(model_key).get("id", "")
        spawn(
            record_and_maybe_dream(ws_path, model_id=model_id),
            name="local-dream-consolidation",
        )
    except Exception as e:  # never let memory bookkeeping disrupt a turn
        log.debug("Could not spawn local dream consolidation: %s", e)


def _spawn_memory_hooks(
    model_key: str, messages: list[dict], session_id: str, ws_path: str | None
) -> None:
    """Post-turn memory hooks for local turns, mirroring the cloud paths:
    session memory (summarised on-device, fully offline) + auto-memory
    extraction and dream consolidation (cloud agents; skipped when fully
    offline)."""
    _spawn_session_update(model_key, messages, session_id)
    _spawn_extraction(model_key, messages, session_id, ws_path)
    _spawn_dream(model_key, ws_path)


def _usage_line(model_key: str, output_tokens: int) -> str:
    return (
        "data: "
        + ndjson_dumps(
            {
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": output_tokens,
                    "total_input": 0,
                    "total_output": output_tokens,
                    "estimated_cost_usd": 0.0,
                    "model": model_key,
                }
            }
        )
        + "\n\n"
    )


def _results_context(tool_results: list[dict], names_by_id: dict[str, str]) -> str:
    """Render executed tool results as a plain-text context message. We feed
    results back as ordinary context (not Gemma's native tool-message shapes),
    which the small model handles more reliably."""
    blocks = []
    for r in tool_results:
        name = names_by_id.get(r.get("tool_use_id", ""), "tool")
        blocks.append(f"[{name} result]\n{r.get('content', '')}")
    return (
        "Results from the tool call(s):\n\n"
        + "\n\n".join(blocks)
        + "\n\nUse these results to answer my previous question directly. "
        "Call another tool only if it is essential."
    )


# ── tools-on path ─────────────────────────────────────────────────────────────


async def _stream_round(model_key: str, convo: list[dict], schemas: list[dict]):
    """Bridge one streaming generation round (sync, thread-affine) to async.
    Pumps ``iter_generate_round``'s pieces onto a queue so the answer streams
    token-by-token as it decodes. Yields ``("text", piece)`` then ``("raw",
    full_text)``; raises on a generation error so the caller can surface it.

    Cancellation: local generation runs on the SINGLE model thread, so an
    abandoned round (client disconnect / Stop) must be stopped or the next turn
    can't start until it decodes to ``max_tokens``. We hand ``iter_generate_round``
    a ``threading.Event`` and set it from the ``finally`` below, which runs when
    this async generator is torn down (``GeneratorExit`` / ``CancelledError``
    propagates in from the drained consumer). The producer's token loop sees the
    flag and breaks, freeing the model thread. On the happy path the event is set
    only after the producer has already finished, so it is a harmless no-op."""
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()

    def produce() -> None:
        try:
            for kind, piece in local_llm.iter_generate_round(
                model_key, convo, schemas, 4096, cancel=cancel
            ):
                loop.call_soon_threadsafe(queue.put_nowait, (kind, piece))
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

    loop.run_in_executor(local_llm.executor, produce)
    try:
        while True:
            kind, piece = await queue.get()
            if kind == "done":
                break
            if kind == "error":
                raise RuntimeError(piece)
            yield (kind, piece)
    finally:
        # Torn down (disconnect/Stop) or exhausted — either way tell the producer
        # to stop decoding so the single model thread is released promptly.
        cancel.set()


async def _tool_loop(
    model_key: str,
    convo: list[dict],
    tool_ctx: dict,
    *,
    session_id: str,
    start_round: int = 0,
    memory_ctx: dict | None = None,
):
    """The agentic loop, shared by fresh turns and approval resumes. Yields SSE
    chunks. On a pending approval / user question it stashes state in
    ``_local_paused`` and ends the stream; the resume picks up from the next
    round."""
    schemas, valid_names = get_tool_schemas(
        plan_mode=tool_ctx.get("plan_mode", False),
        ws_connected=tool_ctx.get("ws_connected", False),
        mcp_enabled_names=tool_ctx.get("mcp_enabled_names"),
        scope=tool_ctx.get("tool_scope", "all"),
        suppress_workspace_search=tool_ctx.get("suppress_ws_search", False),
    )
    out_chars = 0
    # Completion-gate parity with the cloud/GPT paths, bounded per turn.
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
        # Stream the round as it decodes (displayable text only — the tool-call
        # DSL is withheld by the splitter); `raw` carries the full text to parse.
        raw = ""
        async for kind, piece in _stream_round(model_key, convo, schemas):
            if kind == "text":
                out_chars += len(piece)
                yield f"data: {ndjson_dumps({'text': piece})}\n\n"
            else:  # ("raw", full_text)
                raw = piece
        log.info("Local tools round %d output (first 200): %r", rnd, raw[:200])

        calls = parse_tool_calls(raw, valid_names)
        if not calls:
            # Completion gate — Stop hooks + goal evaluator can keep the turn
            # going toward the goal (bounded by the consecutive-block cap).
            _is_last = rnd == _MAX_TOOL_ROUNDS - 1
            if not _is_last and stop_blocks_used < _goal_cap:
                from server.goals import GateContext
                from server.goals.gate import run_completion_gate

                _gate = await run_completion_gate(
                    GateContext(
                        session_id=session_id,
                        messages=convo,
                        goal=_goal_text,
                        provider="local",
                        model_id=model_key,
                        attempt=stop_blocks_used,
                        max_consecutive_blocks=_goal_cap,
                    )
                )
                if _gate.frame:
                    yield f"data: {ndjson_dumps(_gate.frame)}\n\n"
                if _gate.block:
                    stop_blocks_used += 1
                    if raw.strip():
                        convo.append({"role": "assistant", "content": raw})
                    convo.append({"role": "user", "content": f"[completion gate] {_gate.feedback}"})
                    continue
            # The answer already streamed above; just close out the turn.
            yield _usage_line(model_key, out_chars // 4)
            yield "data: [DONE]\n\n"
            return

        # The model's tool-calling turn (incl. any streamed preamble + the DSL)
        # goes into history.
        convo.append({"role": "assistant", "content": raw})

        tool_uses = [gemma_call_to_tool_use(name, args) for name, args in calls]
        names_by_id = {tu["id"]: tu["name"] for tu in tool_uses}
        for tu in tool_uses:
            yield f"data: {ndjson_dumps({'skill': tu['name']})}\n\n"
            yield f"data: {ndjson_dumps({'skill_input': tu['name'], 'input': tu['input']})}\n\n"

        tool_results, sse_events, has_pending_approval, has_user_question = await run_tool_round(
            tool_uses,
            session_id=session_id,
            plan_mode=tool_ctx.get("plan_mode", False),
            mode=tool_ctx.get("mode", "default"),
            session_approvals=tool_ctx.get("session_approvals"),
            session_denials=tool_ctx.get("session_denials"),
            config=tool_ctx.get("config"),
            transcript=tool_ctx.get("transcript", ""),
        )
        # Forward everything process_tool_results produced (skill_result,
        # approval_request, ws_auto_applied, todo_update, ...) verbatim.
        for ev in sse_events:
            yield f"data: {ev}\n\n"

        if has_pending_approval or has_user_question:
            # Hard stop. No destructive action has run — the handler only
            # returned a sentinel. Stash the Gemma conversation so the approval
            # continuation can fill the result and resume from the next round.
            _local_paused[session_id] = {
                "model_key": model_key,
                "convo": convo,
                "tool_ctx": tool_ctx,
                "pending_results": tool_results,
                "names_by_id": names_by_id,
                "next_round": rnd + 1,
                # Carried so the RESUME (not this pause) fires the post-turn
                # memory hooks at the true end of the turn.
                "memory_ctx": memory_ctx,
            }
            yield "data: [DONE]\n\n"
            return

        # All results ready (read-only and/or pre-approved) — feed them back.
        convo.append({"role": "user", "content": _results_context(tool_results, names_by_id)})

    # Ran out of rounds without a final answer.
    yield f"data: {ndjson_dumps({'text': '(Reached the local tool-call round limit without a final answer.)'})}\n\n"
    yield _usage_line(model_key, out_chars // 4)
    yield "data: [DONE]\n\n"


async def resume_local_chat(
    session_id: str, approved_tool_result, session_approvals: dict | None = None
):
    """Resume a paused local tool turn after the user approved / denied an
    action. The action itself already ran server-side via /api/approval/execute
    (the one write path) — here we only inject its result text back into the
    Gemma conversation and continue the loop. Falls back to a clean [DONE] if no
    paused state exists (e.g. after a restart)."""
    paused = _local_paused.pop(session_id, None)
    if not paused:
        log.warning("resume_local_chat: no paused state for %s — ending.", session_id)
        yield "data: [DONE]\n\n"
        return

    answers = (
        approved_tool_result if isinstance(approved_tool_result, list) else [approved_tool_result]
    )
    answers = [a for a in answers if isinstance(a, dict)]
    by_id = {a.get("tool_use_id", ""): a for a in answers}

    tool_results = paused["pending_results"]
    names_by_id = paused["names_by_id"]
    # Replace the awaiting-approval placeholders with the approved outcome text.
    for r in tool_results:
        ans = by_id.get(r.get("tool_use_id", ""))
        if ans is not None:
            r["content"] = ans.get("content", "")
            name = names_by_id.get(r["tool_use_id"], "tool")
            preview = str(r["content"])[:2000]
            yield f"data: {ndjson_dumps({'skill_result': name, 'output': preview})}\n\n"

    convo = paused["convo"]
    convo.append({"role": "user", "content": _results_context(tool_results, names_by_id)})

    tool_ctx = paused["tool_ctx"]
    if session_approvals is not None:
        # Pick up any new "allow for session" choices for downstream rounds.
        tool_ctx = {**tool_ctx, "session_approvals": session_approvals}

    mem = paused.get("memory_ctx")
    async for chunk in _tool_loop(
        paused["model_key"],
        convo,
        tool_ctx,
        session_id=session_id,
        start_round=paused["next_round"],
        memory_ctx=mem,
    ):
        yield chunk
    # The turn is truly done now (unless it paused AGAIN for another approval,
    # in which case the next resume fires the hooks). Deferred counterpart of
    # the completion pass in stream_local_chat.
    if mem and not has_local_pause(session_id):
        _spawn_memory_hooks(paused["model_key"], mem["messages"], session_id, mem["ws_path"])


# ── plain / thinking path + entry point ───────────────────────────────────────


async def stream_local_chat(
    model_key: str,
    system_prompt: str,
    messages: list[dict],
    session_id: str,
    thinking: bool = False,
    tools: bool = False,
    tool_ctx: dict | None = None,
    ws_path: str | None = None,
):
    if tools and local_llm.supports_tools(model_key):
        convo = local_llm.to_chat_messages(system_prompt, messages)
        try:
            async for chunk in _tool_loop(
                model_key,
                convo,
                tool_ctx or {},
                session_id=session_id,
                memory_ctx={"messages": messages, "ws_path": ws_path},
            ):
                yield chunk
        except Exception as e:  # surface load/runtime errors to the client
            log.warning("Local tool chat (%s) failed: %s", model_key, e)
            yield f"data: {ndjson_dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"
            return
        # Don't run the memory hooks on a turn that paused for approval — it
        # isn't finished; the resume fires them at the true end of the turn
        # (the memory_ctx stashed above carries the context there).
        if not has_local_pause(session_id):
            _spawn_memory_hooks(model_key, messages, session_id, ws_path)
        return

    # Plain / thinking path — pump the blocking generator through a queue.
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    # Cooperative cancel: set on teardown so the model thread stops decoding
    # promptly instead of running to max_tokens and wedging the next turn (the
    # single model thread). See _stream_round for the same pattern.
    cancel = threading.Event()

    def produce() -> None:
        """Runs on local_llm.executor — pushes (kind, payload) pieces back to the
        event loop. kind ∈ text|thinking."""
        try:
            for kind, piece in local_llm.iter_chat(
                model_key, system_prompt, messages, thinking=thinking, cancel=cancel
            ):
                loop.call_soon_threadsafe(queue.put_nowait, (kind, piece))
        except Exception as e:  # surface load/runtime errors to the client
            log.warning("Local chat (%s) failed: %s", model_key, e)
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(e)))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))

    loop.run_in_executor(local_llm.executor, produce)

    output_tokens = 0
    errored = False
    thinking_open = False
    try:
        while True:
            kind, value = await queue.get()
            if kind == "thinking":
                if not thinking_open:
                    thinking_open = True
                    yield f"data: {ndjson_dumps({'thinking_start': True})}\n\n"
                yield f"data: {ndjson_dumps({'thinking': value})}\n\n"
            elif kind == "text":
                if thinking_open:
                    thinking_open = False
                    yield f"data: {ndjson_dumps({'thinking_stop': True})}\n\n"
                output_tokens += 1  # chunk-level; good enough for a $0 local counter
                yield f"data: {ndjson_dumps({'text': value})}\n\n"
            elif kind == "error":
                errored = True
                yield f"data: {ndjson_dumps({'error': value})}\n\n"
            else:  # done
                break
    finally:
        # Disconnect/Stop tears this generator down mid-drain — release the model
        # thread. On normal completion the producer is already done (no-op).
        cancel.set()
    if thinking_open:  # thought but no answer text followed
        yield f"data: {ndjson_dumps({'thinking_stop': True})}\n\n"

    if not errored:
        yield _usage_line(model_key, output_tokens)
    yield "data: [DONE]\n\n"
    if not errored:
        _spawn_memory_hooks(model_key, messages, session_id, ws_path)
