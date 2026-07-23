"""Bridge from the chat endpoint to the OpenAI-on-Bedrock runtime.

Mirrors server/local/route.py: the chat endpoint calls ``openai_chat_response``
once, right after the system prompt + messages are built (BEFORE compaction —
compaction calls Bedrock and must never run for an OpenAI turn). Returns a
``StreamingResponse`` for an OpenAI model (fresh turn or approval resume), or
``None`` to let the default Bedrock/Anthropic path proceed.
"""

from __future__ import annotations

from fastapi.responses import StreamingResponse


def openai_chat_response(
    *,
    model_key: str,
    model_id: str,
    body: dict,
    messages: list[dict],
    session_id: str,
    approved_tool_result,
    transcript: str = "",
    system_prompt: str,
    effort_label: str | None,
    plan_mode: bool,
    mode: str,
    ws_path,
    mcp_enabled_names,
    session_approvals: dict,
    session_denials: dict,
    session_config: dict,
    suppress_ws_search: bool = False,
) -> StreamingResponse | None:
    from server.openai_bedrock.runtime import (
        is_openai_model,
        reasoning_effort_for,
        verbosity_for,
    )

    if not is_openai_model(model_key):
        return None

    from server.openai_bedrock.stream import (
        has_openai_pause,
        resume_openai_chat,
        stream_openai_chat,
    )

    # Approval continuation for a paused OpenAI tool turn: the action already
    # ran server-side via /api/approval/execute, so resume the loop with the
    # result. (The Anthropic approved_tool_result path uses a separate
    # _paused_sessions dict, so it never touches this state.)
    if approved_tool_result and has_openai_pause(session_id):
        return StreamingResponse(
            resume_openai_chat(session_id, approved_tool_result, session_approvals),
            media_type="text/event-stream",
        )

    # Tool context mirrors what the cloud loop computes, so the OpenAI model sees
    # the same tool pool and reuses the same executor + approval pipeline.
    tool_ctx = {
        "plan_mode": plan_mode,
        "mode": mode,
        "session_id": session_id,
        "ws_connected": bool(ws_path),
        "mcp_enabled_names": mcp_enabled_names,
        "session_approvals": session_approvals,
        "session_denials": session_denials,
        "config": session_config,
        "suppress_ws_search": suppress_ws_search,
        "transcript": transcript,
    }
    return StreamingResponse(
        stream_openai_chat(
            model_key=model_key,
            model_id=model_id,
            system_prompt=system_prompt,
            messages=messages,
            session_id=session_id,
            effort=reasoning_effort_for(model_key, effort_label),
            verbosity=verbosity_for(model_key, body),
            tool_ctx=tool_ctx,
            ws_path=ws_path,
        ),
        media_type="text/event-stream",
    )
