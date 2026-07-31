"""Local-mode tool glue — full tool parity for the on-device model.

The on-device agentic loop reuses the SAME machinery as the cloud Claude path:
the tool pool, the executor, the permission checks, and the approval system.
NOTHING here is copied — we only bridge between the model server's
OpenAI-shaped calls and the existing pipeline:

  1. ``get_tool_schemas`` exposes the full cloud tool pool in OpenAI function
     format, which is what llama-server expects.
  2. ``run_tool_round`` runs the parsed calls through ``execute_tool_batch`` +
     ``process_tool_results`` — the exact same two calls the cloud loop makes.

Parsing the model's raw output is NOT done here any more: llama-server owns it
via the model's own chat template, so there is no per-family marker code to
maintain. The OpenAI-on-Bedrock path shares ``run_tool_round`` too.

SAFETY INVARIANT (load-bearing — do not break):
    Destructive tools (write / delete / cli) never mutate anything inside the
    executor. Their handlers return a ``[WS_APPROVAL]`` sentinel string that is
    ONLY honoured by ``process_tool_results`` (not by ``execute_tool_batch``).
    So every tool result MUST flow through ``process_tool_results`` and a
    returned ``has_pending_approval`` MUST be treated as a hard stop. We never
    read ``state.output`` directly for writes, and we never execute an approved
    action here — the one write path is the frontend's ``/api/approval/execute``
    endpoint (model-agnostic), which the approval card calls on "Yes".

Local mode stays fully offline: ``run_tool_round`` passes ``model_id=""`` so the
permission LLM explainer never fires (it is gated on a truthy model_id). The
auto-mode classifier is gated the same way, so under the ``auto`` permission
mode the offline path falls back to asking instead of calling Bedrock.
"""

from __future__ import annotations

import logging

from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")


# ── Tool declarations ────────────────────────────────────────────────────────


def _to_openai_fn(anthropic_tool: dict) -> dict:
    """An Anthropic tool ({name, description, input_schema}) → an OpenAI
    function schema. ``input_schema`` is already plain JSON Schema, so it drops
    straight into ``parameters``."""
    return {
        "type": "function",
        "function": {
            "name": anthropic_tool["name"],
            "description": anthropic_tool.get("description", ""),
            "parameters": anthropic_tool.get("input_schema")
            or {"type": "object", "properties": {}},
        },
    }


def get_tool_schemas(
    plan_mode: bool = False,
    ws_connected: bool = False,
    suppress_workspace_search: bool = False,
) -> tuple[list[dict], set[str]]:
    """The full tool pool in OpenAI function format — cloud parity, no scopes.

    The old off/core/core_web/all scope gate is gone: a tool-capable local
    model always sees the same pool the cloud paths get. The pool is a stable
    prompt prefix, so llama-server's prefix cache absorbs the prefill cost
    after the first turn; the 32K default context accommodates the schemas.

    Returns ``(schemas, valid_names)``. ``valid_names`` discards hallucinated
    tool names the model might emit."""
    from server.chat.tool_pool import assemble_tool_pool

    pool = assemble_tool_pool(
        plan_mode=plan_mode,
        ws_connected=ws_connected,
        suppress_workspace_search=suppress_workspace_search,
    )
    schemas = [_to_openai_fn(t) for t in pool]
    names = {s["function"]["name"] for s in schemas}
    return schemas, names


# ── Execution through the existing pipeline (the safety gate) ─────────────────


async def run_tool_round(
    tool_uses: list[dict],
    *,
    session_id: str,
    plan_mode: bool = False,
    mode: str = "default",
    session_approvals: dict | None = None,
    session_denials: dict | None = None,
    config: dict | None = None,
    transcript: str = "",
    model_id: str = "",
) -> tuple[list[dict], list[str], bool, bool]:
    """Execute parsed tool calls through the EXISTING executor + approval
    pipeline — the same two calls the cloud loop makes (routes.py:1076 / 1116).

    Returns ``(tool_results, sse_events, has_pending_approval, has_user_question)``:
      - ``tool_results``: ``[{type, tool_use_id, content}]`` — ``content`` is the
        text to feed back to the model (already budget-trimmed).
      - ``sse_events``: ndjson strings to forward to the client verbatim
        (skill_result, approval_request, ws_auto_applied, todo_update, ...).
      - the two booleans are hard-stop signals (approval / user question).

    ``model_id=""`` keeps local mode offline (no permission-explainer Bedrock
    call). The ``[WS_APPROVAL]`` gate inside ``process_tool_results`` is what
    actually protects destructive tools, and it does not depend on the model id.
    """
    import asyncio

    from server.chat import executor as tool_thread_pool
    from server.chat.budget import make_budget_tool_result
    from server.chat.tool_pool import _is_tool_concurrent_safe
    from server.tool_executor import execute_tool_batch, process_tool_results

    loop = asyncio.get_event_loop()
    states = await execute_tool_batch(
        tool_uses,
        is_concurrent_safe=_is_tool_concurrent_safe,
        loop=loop,
        executor=tool_thread_pool,
        transcript=transcript,
        attachments={},
        session_id=session_id,
        session_denials=session_denials or {},
        # "" for the local path (offline: no classifier/explainer Bedrock
        # call); the OpenAI caller passes its real id so spawn_agent /
        # team_create inherit the SESSION model instead of silently falling
        # back to the default chat model.
        model_id=model_id,
        plan_mode=plan_mode,
        mode=mode,
    )

    truncation_events: list[dict] = []
    tool_results, sse_events, has_pending_approval, has_user_question = await process_tool_results(
        states,
        budget_fn=make_budget_tool_result(truncation_events),
        session_approvals=session_approvals or {},
        config=config,
        model_id=model_id,
        recent_messages=[],
        mode=mode,
    )
    for ev in truncation_events:
        sse_events.append(ndjson_dumps(ev))
    return tool_results, sse_events, has_pending_approval, has_user_question
