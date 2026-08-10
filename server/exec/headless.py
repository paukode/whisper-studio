"""``run_headless_turn`` — a Python-callable, non-streaming primitive over the
shared agentic turn engine (server.chat.engine.runner.run_turn).

This is the internal equivalent of what a future ``codex exec``-style CLI or
SDK would wrap: BACKEND code (a future terminal-panel helper, an internal
script) drives one turn to completion with no human attending it and no HTTP
client involved. There is deliberately no network route here — this module is
imported and awaited directly, the same way server/agents/runtime.py's
run_agent() is called in-process today.

It mirrors server/agents/runtime.py's ``_run_agent_loop`` almost exactly: the
same ``unattended=True`` + fresh ``turn_scope_id`` + ``tool_catalog`` closure
construction of TurnContext, the same provider-adapter selection
(``_get_chat_model_meta`` -> Anthropic or OpenAI-on-Bedrock), and the same
"drain run_turn's pinned SSE ndjson frames" parsing loop. Read that function
first if you're touching this one.

Two differences from ``_run_agent_loop``, both deliberate:

  - The tool catalog is the same core+activated pool interactive chat gets
    via progressive tool disclosure (``assemble_partitioned_pool``, built
    ONCE here rather than reassembled per round) — no ``filter_tools_for_agent``
    narrowing and no ``get_agent_runtime_tools`` (spawn_agent/send_message/
    complete_coordination): those are agent-hierarchy concepts a standalone
    headless run has no use for.
  - On-device (``local:...``) models are refused outright rather than wired
    up: ``_distill_structured`` below (reused unmodified, per its own "not
    migrated" docstring in runtime.py) goes through the OLD
    ``server.agents.providers.get_adapter``, which has no local branch — the
    exact reason ``_resolve_agent_model`` already excludes local candidates
    from its own fallback chain. Full local support additionally needs the
    llama-server cold-start dance (see server/local/route.py) and capability
    probing (supports_tools/supports_thinking) that is a materially separate
    piece of work, out of this primitive's scope.

Event vocabulary
-----------------
``run_headless_turn`` is an async generator yielding a small, STABLE dict
vocabulary — this function's OWN contract, deliberately distinct from
runner.py's pinned internal SSE frames (see that module's docstring on
golden-fixture pinning; those frames can reshape independently of this one):

    {"type": "text", "text": str}
        One flushed chunk of assistant text for a completed round (buffered
        per round, exactly like _run_agent_loop's own "text" progress event —
        not one per streamed delta). A ``notify_user`` tool call is folded in
        here too (its message text), mirroring how _run_agent_loop's
        ``_finalize`` merges notify_user into the agent's returned output.
    {"type": "tool_call", "name": str, "input": dict}
        A tool the model invoked, with its full (model-visible) input —
        unlike _run_agent_loop's progress events this is not preview-
        truncated, since the consumer here is code, not a UI card.
    {"type": "tool_result", "name": str, "output": str, "status": "ok" | "error"}
        That tool call's result.
    {"type": "usage", "input_tokens": int, "output_tokens": int,
     "cache_read_tokens": int, "cache_creation_tokens": int}
        Cumulative usage totals as of the latest completed round. Emitted
        once per round, and once more at the end if ``output_schema`` added
        an extra structured-output call — the LAST usage event is always the
        authoritative running total.
    {"type": "error", "message": str}
        A terminal round-level error surfaced by the engine, or a pre-flight
        failure (unknown model_key, no cloud model configured, local model
        requested). Always followed by a "done" event with status="failed"
        for pre-flight failures.
    {"type": "structured", "output": dict}
        Present only when ``output_schema`` was given and the one-shot
        distillation call (_distill_structured) produced a schema-valid
        result. Absent (not an error) if validation failed twice.
    {"type": "done", "status": "completed" | "turn_limit" | "failed",
     "session_id": str}
        Always the last event. "turn_limit" means the round budget was
        exhausted before a natural finish (mirrors AgentResult.stopped_early).
        "failed" means a pre-flight check stopped the run before any turn ran.

A raised exception from inside the turn itself (as opposed to a frame-level
"error") propagates to the caller rather than being swallowed — this mirrors
_run_agent_loop's own body (no broad try/except there either; that lives one
level up, in run_agent's orchestration shell, which this primitive has no
equivalent of).
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

log = logging.getLogger("whisper-studio")

# Dedicated small pool for headless Bedrock calls — mirrors
# server.agents.runtime._agent_executor (its own dedicated pool), so a
# headless run's blocking provider calls never head-of-line-block the chat
# route's or the agent runtime's own executors.
_HEADLESS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="headless-exec")

# Matches CHAT_POLICY's own default (server/chat/engine/policy.py) — a
# reasonable ceiling when the caller doesn't specify one.
DEFAULT_MAX_ROUNDS = 50


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_headless_turn(
    prompt: str,
    *,
    workspace_path: str | None = None,
    model_key: str | None = None,
    output_schema: dict | None = None,
    ephemeral: bool = False,
    session_id: str | None = None,
    max_rounds: int | None = None,
) -> AsyncIterator[dict]:
    """Run one full agentic turn headlessly and yield progress/result events.

    Args:
        prompt: The user-turn text handed to the model.
        workspace_path: Workspace to run against for this call only (via the
            same context-local override worktree-isolated agents already use,
            server.workspace.state.set_workspace_override) — never mutates
            the process-wide connected workspace. None means "whatever
            workspace is currently connected, if any" (same as an agent run).
        model_key: A ``chat_models`` config key. None resolves the user's
            default chat model via the exact fallback chain
            server.agents.runtime._resolve_agent_model already implements
            (skips on-device candidates). An on-device model, explicit or
            resolved, is refused — see the module docstring.
        output_schema: When given, one extra one-shot structured-output call
            runs after the turn via server.agents.runtime._distill_structured
            (with its own single schema-repair retry).
        ephemeral: When True, this run's session id is never written to
            sessions.db — no row is created, no message is appended. When
            False (default), the prompt and the final answer are appended via
            server.infrastructure.sessions.append_message, the same
            out-of-band write path background tasks already use to persist
            into a session with no frontend attached (that helper lazily
            creates the row on first write, so a brand new session_id works).
        session_id: Reused verbatim if given; otherwise a fresh uuid4 hex.
            Note this is a real sessions.db key when ephemeral=False, so a
            caller-supplied id must not collide with a live chat session it
            doesn't intend to append into.
        max_rounds: Caps model rounds this turn gets. None uses
            DEFAULT_MAX_ROUNDS.

    Yields:
        Event dicts per the vocabulary documented in this module's docstring.
    """
    from server.agents.config import get_agent_config
    from server.agents.providers import model_key_for_id
    from server.agents.runtime import _distill_structured, _resolve_agent_model
    from server.chat.infra import _get_chat_model_meta
    from server.chat.tool_activation import activate_from_history
    from server.chat.tool_index import build_deferred_index
    from server.chat.tool_pool import assemble_partitioned_pool
    from server.infrastructure.config import load_config
    from server.local.runtime import is_local_model_id
    from server.workspace import get_workspace_path
    from server.workspace.state import reset_workspace_override, set_workspace_override

    session_id = session_id or uuid.uuid4().hex
    turn_scope_id = f"exec:{session_id}"

    # ── Model resolution ──────────────────────────────────────────────────
    cfg = load_config()
    chat_models = cfg.get("chat_models", {})
    _agent_cfg = get_agent_config("general")

    if model_key:
        if model_key not in chat_models:
            log.warning("run_headless_turn: unknown chat model key %r", model_key)
            yield {"type": "error", "message": f"Unknown chat model key: {model_key!r}"}
            yield {"type": "done", "status": "failed", "session_id": session_id}
            return
        resolved_model_key = model_key
        model_id = chat_models[model_key]
    else:
        model_id = _resolve_agent_model(None, _agent_cfg)
        if not model_id:
            log.warning("run_headless_turn: cannot run, every configured chat model is on-device")
            yield {
                "type": "error",
                "message": (
                    "No cloud chat model configured: every chat_models entry is "
                    "on-device (local:*), which run_headless_turn cannot use."
                ),
            }
            yield {"type": "done", "status": "failed", "session_id": session_id}
            return
        resolved_model_key = model_key_for_id(model_id)

    if is_local_model_id(model_id):
        log.warning("run_headless_turn: refusing on-device model %r", model_id)
        yield {
            "type": "error",
            "message": (
                f"On-device model {model_id!r} is not supported by run_headless_turn "
                "(structured-output distillation has no local adapter branch, and full "
                "local support needs its own cold-start/capability wiring). Pick a cloud "
                "model_key."
            ),
        }
        yield {"type": "done", "status": "failed", "session_id": session_id}
        return

    # ── Workspace override (this call only; never touches the global one) ──
    _ws_token = set_workspace_override(workspace_path) if workspace_path is not None else None
    try:
        ws_path = get_workspace_path()

        if not ephemeral:
            from server.infrastructure.sessions import append_message

            await append_message(
                session_id,
                {"role": "user", "content": prompt, "timestamp": _utc_now_iso()},
            )

        # The turn's only user message, built here (rather than down by
        # ctx = TurnContext(...)) so it's already in scope for
        # activate_from_history below; reused verbatim for the TurnContext.
        messages = [{"role": "user", "content": prompt}]

        # ── Tool catalog: core + this session's activated tools, built ONCE
        # for the whole run ─────────────────────────────────────────────────
        # Progressive tool disclosure: re-derive this session's activations
        # from visible history first (self-healing across restarts, exactly
        # like server/chat/routes.py), then the same call agents/runtime.py
        # makes (assemble_partitioned_pool(plan_mode=False, ws_connected=...,
        # session_id=...)) but WITHOUT filter_tools_for_agent or
        # get_agent_runtime_tools — see the module docstring. Everything
        # deferred is folded into a compact index appended to the system
        # prompt below.
        activate_from_history(session_id, messages)
        advertised, deferred, _core_count = assemble_partitioned_pool(
            plan_mode=False, ws_connected=bool(ws_path), session_id=session_id
        )
        deferred_tool_index = build_deferred_index(deferred)
        seen_names: set[str] = set()
        tools: list[dict] = []
        for t in advertised:
            if t["name"] not in seen_names:
                seen_names.add(t["name"])
                tools.append(t)

        def _tool_catalog() -> tuple[list[dict], int | None]:
            return tools, None

        system = (
            "You are completing a single headless task with no human attending this "
            "session and no chat UI. Use the available tools directly, make reasonable "
            "assumptions instead of asking clarifying questions (nobody will answer), "
            "and give a complete, self-contained final answer."
        )
        if not ws_path:
            system += (
                "\n\nNo workspace folder is connected. File tools (ws_read_file, ws_grep, "
                "ws_glob, etc.) are not available."
            )
        if deferred_tool_index:
            system += "\n\n" + deferred_tool_index

        # ── Provider adapter — same chat_model_meta.provider check
        # server/chat/routes.py and agents/runtime.py both use. ─────────────
        loop = asyncio.get_event_loop()
        _provider = _get_chat_model_meta().get(resolved_model_key, {}).get("provider", "anthropic")
        if _provider == "openai_bedrock":
            from server.chat.engine.openai import OpenAIResponsesAdapter

            adapter = OpenAIResponsesAdapter(
                model_key=resolved_model_key,
                model_id=model_id,
                system_prompt=system,
                effort_label=None,
                session_id=session_id,
            )
        else:
            from server.chat.engine.anthropic import AnthropicAdapter

            adapter = AnthropicAdapter(
                model_key=resolved_model_key,
                model_id=model_id,
                system_prompt=system,
                system_static="",
                system_dynamic="",
                caching_on=False,
                cache_ttl="5m",
                effort_label=None,
                force_skill=None,
                loop=loop,
                executor=_HEADLESS_EXECUTOR,
            )

        from server.chat.engine.policy import TurnPolicy
        from server.chat.engine.runner import TurnContext, run_turn

        ctx = TurnContext(
            session_id=session_id,
            model_key=resolved_model_key,
            model_id=model_id,
            messages=messages,
            adapter=adapter,
            policy=TurnPolicy(
                max_rounds=max_rounds or DEFAULT_MAX_ROUNDS,
                completion_gate=False,
                salvage_round=True,
            ),
            loop=loop,
            executor=_HEADLESS_EXECUTOR,
            tool_exec_model_id=model_id,
            memory_hooks=lambda msgs: None,
            unattended=True,
            turn_scope_id=turn_scope_id,
            tool_catalog=_tool_catalog,
        )

        # ── Drain run_turn: an async generator yielding SSE-format ndjson
        # strings ("data: {...}\n\n" / "data: [DONE]\n\n" / ": hb\n\n"
        # heartbeat comments). THIS PARSING IS COUPLED TO runner.py's PINNED
        # SSE FRAME VOCABULARY, exactly like _run_agent_loop's own drain —
        # see that function's comment before touching this one. ────────────
        rounds_used = 0
        all_text_parts: list[str] = []
        round_text_parts: list[str] = []
        final_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }

        def _flush_round_text() -> str | None:
            if not round_text_parts:
                return None
            text = "".join(round_text_parts)
            round_text_parts.clear()
            if not text.strip():
                return None
            all_text_parts.append(text)
            return text

        try:
            async for chunk in run_turn(ctx):
                for raw_line in chunk.splitlines():
                    if not raw_line.startswith("data: "):
                        continue  # heartbeat comments (": hb") and blank lines
                    payload = raw_line[len("data: ") :]
                    if payload == "[DONE]":
                        continue
                    try:
                        frame = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    if "usage" in frame:
                        u = frame["usage"]
                        final_usage = {
                            "input_tokens": u.get("total_input", 0),
                            "output_tokens": u.get("total_output", 0),
                            "cache_read_tokens": u.get("total_cache_read", 0),
                            "cache_creation_tokens": u.get("total_cache_creation", 0),
                        }
                        rounds_used += 1
                        flushed = _flush_round_text()
                        if flushed:
                            yield {"type": "text", "text": flushed}
                        yield {"type": "usage", **final_usage}
                    elif "text" in frame and isinstance(frame.get("text"), str):
                        round_text_parts.append(frame["text"])
                    elif "error" in frame:
                        yield {"type": "error", "message": frame["error"]}
                    elif "skill_input" in frame:
                        tool_name = frame["skill_input"]
                        yield {
                            "type": "tool_call",
                            "name": tool_name,
                            "input": frame.get("input") or {},
                        }
                    elif "skill_result" in frame:
                        tool_name = frame["skill_result"]
                        output = frame.get("output") or ""
                        yield {
                            "type": "tool_result",
                            "name": tool_name,
                            "output": output,
                            "status": "error" if output.startswith("[Tool Error]") else "ok",
                        }
                    elif "notify_user" in frame:
                        msg = (frame["notify_user"] or {}).get("message", "")
                        if msg:
                            all_text_parts.append(msg)
                            yield {"type": "text", "text": msg}
        finally:
            flushed = _flush_round_text()  # catch trailing text with no following usage frame
            if flushed:
                yield {"type": "text", "text": flushed}

        _effective_max_rounds = max_rounds or DEFAULT_MAX_ROUNDS
        stopped_early = rounds_used >= _effective_max_rounds
        collected_text = "\n\n".join(all_text_parts)

        structured = None
        if output_schema is not None:
            from server.agents.providers import TurnUsage as _OldTurnUsage
            from server.agents.providers import get_adapter as _get_old_adapter

            _old_adapter = _get_old_adapter(resolved_model_key, model_id)
            _old_usage = _OldTurnUsage(**final_usage)
            if stopped_early:
                # Matches _run_agent_loop's capped-exit path: no synthetic
                # assistant turn appended, ctx.messages already ends with the
                # last tool result.
                _distill_messages = ctx.messages
            else:
                _distill_messages = [
                    *ctx.messages,
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": collected_text or "(done)"}],
                    },
                ]
            structured = await _distill_structured(
                _old_adapter, system, _distill_messages, output_schema, _agent_cfg, _old_usage
            )
            final_usage = _old_usage.as_dict()
            yield {"type": "usage", **final_usage}
            if structured is not None:
                yield {"type": "structured", "output": structured}

        if not ephemeral:
            from server.infrastructure.sessions import append_message

            final_message: dict = {
                "role": "assistant",
                "content": collected_text,
                "timestamp": _utc_now_iso(),
            }
            if structured is not None:
                final_message["structured_output"] = structured
            await append_message(session_id, final_message)

        yield {
            "type": "done",
            "status": "turn_limit" if stopped_early else "completed",
            "session_id": session_id,
        }
    finally:
        if _ws_token is not None:
            reset_workspace_override(_ws_token)
