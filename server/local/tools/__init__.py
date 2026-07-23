"""Local-mode tool glue — full tool parity for the on-device model.

The local (Gemma) agentic loop reuses the SAME machinery as the cloud Claude
path: the tool pool, the executor, the permission checks, and the approval
system. NOTHING here is copied — we only translate between Gemma's tool-call
dialect and the existing pipeline:

  1. ``get_tool_schemas`` exposes the full cloud tool pool (plus web_search /
     web_fetch, which the cloud advertises natively and so aren't in the pool)
     in Gemma's OpenAI-style function format.
  2. ``parse_tool_calls`` reads Gemma's ``<|tool_call>call:NAME{...}<tool_call|>``
     DSL and ``gemma_call_to_tool_use`` shapes each into a Bedrock-style
     ``tool_use`` block (synthesising the call id the DSL lacks).
  3. ``run_tool_round`` runs them through ``execute_tool_batch`` +
     ``process_tool_results`` — the exact same two calls the cloud loop makes.

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
import re
import uuid

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
    """An Anthropic tool ({name, description, input_schema}) → Gemma's OpenAI
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
    """The tool pool in Gemma's function format, filtered by ``scope``:
      - 'all'      — full cloud pool + web tools (parity; heaviest prompt)
      - 'core'     — just CORE_TOOL_NAMES (no web)
      - 'core_web' — CORE_TOOL_NAMES + web search/fetch

    Returns ``(gemma_schemas, valid_names)``. ``valid_names`` discards
    hallucinated tool names the model might emit. The cloud pool itself is
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


def gemma_call_to_tool_use(name: str, args: dict) -> dict:
    """A parsed Gemma call → a Bedrock-shaped ``tool_use`` block. Gemma's DSL
    carries no call id, so we synthesise one (pool tool names are unique, so
    name-based dispatch downstream is unambiguous)."""
    return {
        "type": "tool_use",
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "name": name,
        "input": args if isinstance(args, dict) else {},
    }


# ── Gemma tool-call DSL parser ────────────────────────────────────────────────
#
# Gemma emits a call as:
#     <|tool_call>call:NAME{key:<|"|>value<|"|>,key2:123,flag:true}<tool_call|>
# We stop generation at <tool_call|>, so the close marker is often absent — match
# either the close marker or end-of-text. One call per round is the norm (we stop
# right after it), so the non-greedy brace body is safe for the common case.
_CALL_RE = re.compile(
    r"<\|tool_call>\s*call:\s*([A-Za-z0-9_.\-]+)\s*\{(.*?)\}\s*(?:<tool_call\|>|$)",
    re.DOTALL,
)
# key:value, where a string value may be wrapped in <|"|>...<|"|>.
_ARG_RE = re.compile(r'([A-Za-z0-9_]+)\s*:\s*(?:<\|"\|>(.*?)<\|"\|>|([^,}]*))', re.DOTALL)


def _coerce(raw: str):
    """Best-effort scalar coercion for un-quoted DSL values."""
    s = raw.strip()
    if s in ("true", "false"):
        return s == "true"
    if s in ("null", "none", "None"):
        return None
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except ValueError:
            pass
    if re.fullmatch(r"-?\d*\.\d+", s):
        try:
            return float(s)
        except ValueError:
            pass
    return s


def _parse_args(body: str) -> dict:
    """Parse a DSL arg body into a dict. Tries strict JSON first (some emissions
    are a plain JSON object), then falls back to the flat key:value DSL. Nested
    object/array args via the DSL are a known limitation — the model can re-emit
    as JSON, and the executor surfaces a clear validation error otherwise."""
    body = (body or "").strip()
    if not body:
        return {}
    import json

    try:
        obj = json.loads("{" + body + "}")
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    args: dict = {}
    for am in _ARG_RE.finditer(body):
        key = am.group(1)
        if am.group(2) is not None:  # quoted string value
            args[key] = am.group(2).strip()
        else:  # bare scalar
            args[key] = _coerce(am.group(3) or "")
    return args


def parse_tool_calls(text: str, valid_names: set[str] | None = None) -> list[tuple[str, dict]]:
    """Parse Gemma's tool-call DSL out of generated text → ``[(name, args), ...]``.

    When ``valid_names`` is given, names not in the set are dropped (guards
    against hallucinated tools). With no set, every well-formed call is returned
    (used by the legacy web-only unit tests)."""
    calls: list[tuple[str, dict]] = []
    for m in _CALL_RE.finditer(text):
        name = m.group(1).strip()
        if valid_names is not None and name not in valid_names:
            log.info("Local model emitted unknown tool %r — ignoring.", name)
            continue
        calls.append((name, _parse_args(m.group(2))))
    return calls


def strip_tool_markers(text: str) -> str:
    """Remove any stray tool-call DSL from text meant for the user."""
    return _CALL_RE.sub("", text).strip()


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
    """Execute parsed Gemma tool calls through the EXISTING executor + approval
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
