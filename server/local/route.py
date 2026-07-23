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
    mcp_enabled_names,
    session_approvals: dict,
    session_denials: dict,
    session_config: dict,
    suppress_ws_search: bool = False,
) -> StreamingResponse | None:
    from server.local.runtime import build_local_system_prompt, is_local_model

    if not is_local_model(model_key):
        return None

    from server.local.stream import has_local_pause, resume_local_chat, stream_local_chat

    # Approval continuation for a paused local tool turn: the action already ran
    # server-side via /api/approval/execute, so just resume the Gemma loop with
    # the result. (The cloud approved_tool_result path uses a separate
    # _paused_sessions dict, so it never touches local state.) Note: the resume
    # reuses the paused turn's tool_ctx, so strict_rag suppression (if it was
    # active on the original turn) intentionally carries over — unlike the cloud
    # resume, which recomputes suppress_ws_search as False.
    if approved_tool_result and has_local_pause(session_id):
        return StreamingResponse(
            resume_local_chat(session_id, approved_tool_result, session_approvals),
            media_type="text/event-stream",
        )

    local_tools_on = bool(body.get("local_tools", False))
    # Tool scope picks how much of the pool the on-device model sees:
    # off / core / core_web / all. Fewer tools = a much smaller prompt = faster.
    tool_scope = str(body.get("local_tool_scope", "all"))
    # Lean system prompt: the cloud one is tool/workspace-heavy (~2-2.5k tokens
    # the local model can't use) — drop it to speed prefill and free n_ctx,
    # keeping only project/memory context.
    local_system = build_local_system_prompt(
        whisper_md_context,
        memory_context,
        session_memory_context,
        tools=local_tools_on,
    )
    # Tool context mirrors what the cloud loop computes per request, so the local
    # model sees the same tool pool and reuses the same executor + approval
    # pipeline. model_id is intentionally absent (local stays offline; the
    # approval gate is structural, not model-dependent).
    tool_ctx = {
        "plan_mode": plan_mode,
        "mode": mode,
        "ws_connected": bool(ws_path),
        "mcp_enabled_names": mcp_enabled_names,
        "session_approvals": session_approvals,
        "session_denials": session_denials,
        "config": session_config,
        "tool_scope": tool_scope,
        "suppress_ws_search": suppress_ws_search,
        "transcript": transcript,
    }
    return StreamingResponse(
        stream_local_chat(
            model_key,
            local_system,
            messages,
            session_id,
            thinking=bool(body.get("local_thinking", False)),
            tools=local_tools_on,
            tool_ctx=tool_ctx,
            ws_path=ws_path,
        ),
        media_type="text/event-stream",
    )
