"""Local-mode tool glue — full tool parity for the on-device model.

The on-device agentic loop reuses the SAME machinery as the cloud Claude path:
the tool pool, the executor, the permission checks, and the approval system.
NOTHING here is copied — we only bridge between the model server's
OpenAI-shaped calls and the existing pipeline:

  1. ``get_tool_schemas`` exposes the full cloud tool pool (plus web_search /
     web_fetch, which the cloud advertises natively and so aren't in the pool)
     in OpenAI function format, which is what llama-server expects.
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
auto-mode classifier only runs if the user has ``auto_mode_enabled`` globally —
off by default; see the morning report for that one caveat.
"""

from __future__ import annotations

import logging

from server.utils import ndjson_dumps

log = logging.getLogger("whisper-studio")


# ── Tool declarations ────────────────────────────────────────────────────────

# web_search / web_fetch are registered executors (server/executors/web.py) but
# are NOT in assemble_tool_pool (the cloud model gets web access natively). The
# local model has no native web, so we declare them explicitly; they dispatch
# through the same execute_tool_batch → route_tool → EXECUTORS path as the rest.
_WEB_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current, up-to-date information — recent events, "
                "weather, prices, news, or anything not in your training data. Returns "
                "the top results with titles, URLs, and snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the readable text content of a web page given its URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL of the page to fetch."}
                },
                "required": ["url"],
            },
        },
    },
]


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


# A small, high-value subset of the pool for the "core" tool scope: the
# everyday agentic actions (read/search/edit files, run commands, inspect git,
# remember things). Declaring only these keeps the prompt ~1.5K tokens instead
# of ~8K, so on-device turns prefill far faster. Writes/commands are still gated
# by the approval system. Names are a subset of assemble_tool_pool's output;
# any not present in a given request (e.g. ws_* without a workspace) are simply
# absent. Web tools are added separately via the 'core_web' / 'all' scopes.
CORE_TOOL_NAMES = {
    "ws_read_file",
    "ws_grep",
    "ws_glob",
    "ws_list_directory",
    "ws_edit_file",
    "ws_write_file",
    "ws_run_command",
    "git_status",
    "git_diff",
    "memory_read",
    "memory_write",
}


def get_tool_schemas(
    plan_mode: bool = False,
    ws_connected: bool = False,
    mcp_enabled_names: set[str] | None = None,
    scope: str = "all",
    suppress_workspace_search: bool = False,
) -> tuple[list[dict], set[str]]:
    """The tool pool in OpenAI function format, filtered by ``scope``:
      - 'all'      — full cloud pool + web tools (parity; heaviest prompt)
      - 'core'     — just CORE_TOOL_NAMES (no web)
      - 'core_web' — CORE_TOOL_NAMES + web search/fetch

    Returns ``(schemas, valid_names)``. ``valid_names`` discards hallucinated
    tool names the model might emit. The cloud pool itself is
    unchanged — this only chooses how much of it the on-device model sees, so
    the user can trade capability for speed."""
    from server.chat.tool_pool import assemble_tool_pool

    pool = assemble_tool_pool(
        plan_mode=plan_mode,
        ws_connected=ws_connected,
        mcp_enabled_names=mcp_enabled_names,
        suppress_workspace_search=suppress_workspace_search,
    )
    if scope in ("core", "core_web"):
        pool = [t for t in pool if t["name"] in CORE_TOOL_NAMES]
    web = list(_WEB_TOOL_SCHEMAS) if scope in ("all", "core_web") else []

    schemas = web + [_to_openai_fn(t) for t in pool]
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
