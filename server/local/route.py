"""Bridge from the chat endpoint to the on-device runtime.

Keeps server/chat/routes.py lean (and under the file-size budget): the endpoint
calls ``local_chat_response(...)`` once, right after the system prompt + messages
are built. This function decides whether the turn is a local (on-device) one —
fresh or an approval resume — and returns the ``StreamingResponse``, or ``None``
to let the cloud Claude path proceed.

This MUST be reached BEFORE compaction in the endpoint: compaction itself calls
Bedrock, and a local turn must never reach it.
"""

from __future__ import annotations

from fastapi.responses import StreamingResponse


def local_chat_response(
    *,
    model_key: str,
    body: dict,
    messages: list[dict],
    session_id: str,
    approved_tool_result,
    transcript: str = "",
    whisper_md_context: str,
    memory_context: str,
    session_memory_context: str,
    plan_mode: bool,
    mode: str,
    ws_path,
    session_approvals: dict,
    session_denials: dict,
    session_config: dict,
    suppress_ws_search: bool = False,
) -> StreamingResponse | None:
    from server.local.runtime import build_local_system_prompt, is_local_model

    if not is_local_model(model_key):
        return None

    from server.local import server_stream

    # Approval continuation for a paused local tool turn: the action already ran
    # server-side via /api/approval/execute, so just resume the loop with the
    # result. (The cloud approved_tool_result path uses a separate
    # _paused_sessions dict, so it never touches local state.) Note: the resume
    # reuses the paused turn's tool_ctx, so strict_rag suppression (if it was
    # active on the original turn) intentionally carries over — unlike the cloud
    # resume, which recomputes suppress_ws_search as False.
    if approved_tool_result and server_stream.has_pause(session_id):
        return StreamingResponse(
            server_stream.resume_chat(session_id, approved_tool_result, session_approvals),
            media_type="text/event-stream",
        )

    from server.local.runtime import supports_thinking, supports_tools

    # No user-facing gates: like the cloud paths, a tool-capable local model
    # always gets the full pool, and a thinking-capable one always thinks. The
    # old per-turn toggles (local_tools / local_tool_scope / local_thinking)
    # are gone; capability flags in the model registry are the only switch, so
    # a model without a capability simply doesn't use it. The full pool is a
    # stable prompt prefix, so llama-server's prefix cache absorbs the prefill
    # cost after the first turn (the 32K default context accommodates it).
    local_tools_on = supports_tools(model_key)
    thinking_on = supports_thinking(model_key)
    # Context size: the composer's CTX slider value rides on EVERY local turn,
    # so the chip is the source of truth and a lazy server start can never run
    # at a stale size. Without this, a backend restart wiped the in-memory
    # sticky value and the next turn silently relaunched llama-server at the
    # registry default while the UI showed the user's larger choice (64K chip,
    # n_ctx=32768 exceed_context_size errors). Clamped like the load endpoint —
    # never trust a client-supplied size. Recorded via set_requested_n_ctx so
    # the non-chat callers (session summariser, index extraction) reuse the
    # same size instead of bouncing the server.
    n_ctx = body.get("local_context_window")
    if n_ctx is not None:
        try:
            n_ctx = max(2048, min(int(n_ctx), 262144))
        except (TypeError, ValueError):
            n_ctx = None
    if n_ctx:
        from server.local.runtime import set_requested_n_ctx

        set_requested_n_ctx(n_ctx)
    # Lean system prompt: the cloud one is tool/workspace-heavy (~2-2.5k tokens
    # the local model can't use) — drop it to speed prefill and free n_ctx,
    # keeping only project/memory context plus, when tools are on, a short marker
    # that a workspace is connected (so the model reaches it via ws_* tools
    # instead of claiming no project was provided).
    local_system = build_local_system_prompt(
        whisper_md_context,
        memory_context,
        session_memory_context,
        tools=local_tools_on,
        ws_path=ws_path or "",
    )
    # Tool context mirrors what the cloud loop computes per request, so the local
    # model sees the same tool pool and reuses the same executor + approval
    # pipeline. model_id is intentionally absent (local stays offline; the
    # approval gate is structural, not model-dependent).
    tool_ctx = {
        "plan_mode": plan_mode,
        "mode": mode,
        "ws_connected": bool(ws_path),
        "session_approvals": session_approvals,
        "session_denials": session_denials,
        "config": session_config,
        "suppress_ws_search": suppress_ws_search,
        "transcript": transcript,
        # Carried so the tools path renders the thinking channel (and a paused
        # turn's resume keeps the same setting), matching the plain path.
        "thinking": thinking_on,
    }
    return StreamingResponse(
        server_stream.stream_chat(
            model_key,
            local_system,
            messages,
            session_id,
            thinking=thinking_on,
            tools=local_tools_on,
            tool_ctx=tool_ctx,
            ws_path=ws_path,
            n_ctx=n_ctx,
        ),
        media_type="text/event-stream",
    )
