"""
Agent runtime — executes an agent with a full tool loop.

Unlike the main chat stream (SSE, streaming), agent runtime uses
non-streaming invoke_model calls in a loop until the agent produces
a final text response or hits its turn limit.
"""

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from server.agents.config import (
    AgentConfig,
    filter_tools_for_agent,
    get_agent_config,
)
from server.agents.event_bus import event_bus
from server.agents.messaging import message_bus
from server.agents.registry import agent_registry
from server.hooks import run_hooks

log = logging.getLogger("whisper-studio")

# Shared executor for agent bedrock calls
_agent_executor = ThreadPoolExecutor(max_workers=4)
# Separate small pool for git/worktree harvesting so a burst of finishing
# agents doing git ops can't head-of-line-block model calls on _agent_executor.
_git_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-git")


def _with_session_id(tool_input: dict, session_id: str) -> dict:
    """Return a COPY of the model's tool input with the internal session id
    injected for the executor.

    The original ``tool_input`` is ``tu["input"]`` — the exact dict that lives
    inside the assistant message replayed to Bedrock on every subsequent turn.
    Mutating it in place would leak the internal ``__session_id__`` key into the
    transcript (where the model can see and imitate it), and some executors only
    ``.get()`` it rather than ``.pop()`` it, so it would persist. Copying keeps
    ``tu["input"]`` pristine, mirroring the main chat path (tool_executor.py).
    """
    call_input = dict(tool_input)
    call_input["__session_id__"] = session_id
    return call_input


def _resolve_agent_model(model_id_override: str | None, config: AgentConfig) -> str | None:
    """Resolve the model id for an agent run.

    An explicit override is returned verbatim (the spawn handlers already
    threaded the session-selected model through it). Without one, fall back to
    the user's configured default chat model, skipping on-device (``local:*``)
    entries: get_adapter has no local branch, so a local id would silently
    route to the Bedrock adapter and fail at invoke. This is how background
    memory agents broke in hybrid mode when the default chat model was local.
    Returns None when every configured chat model is on-device; run_agent
    fails the run early instead of erroring at the provider call.
    """
    if model_id_override:
        return model_id_override

    from server.infrastructure.config import load_config
    from server.local.runtime import is_local_model_id

    cfg = load_config()
    chat_models = cfg.get("chat_models", {})
    default_key = cfg.get("default_chat_model")
    candidates = [
        # config.model is deprecated (always None now) but still honoured
        # first if some caller sets it.
        chat_models.get(config.model) if config.model else None,
        chat_models.get(default_key) if default_key else None,
        chat_models.get("sonnet"),
        *chat_models.values(),
    ]
    for candidate in candidates:
        if candidate and not is_local_model_id(candidate):
            return candidate
    return None


@dataclass
class AgentResult:
    """Result from an agent execution."""

    agent_id: str
    agent_type: str
    output: str
    status: str = "completed"  # completed | failed | stopped
    turns_used: int = 0
    tools_called: list[str] = field(default_factory=list)
    # Aggregated token usage across the agent's turns (provider-normalized:
    # input/output/cache_read/cache_creation). Empty dict when unavailable.
    usage: dict = field(default_factory=dict)
    # Schema-validated result object when the caller passed structured_schema.
    structured_output: dict | None = None
    # True when the loop exited on a turn/time limit rather than a clean finish.
    # status stays "completed" (memory/subagent callers branch on it), but the
    # work is partial — the worktree harvest keeps the worktree instead of
    # applying it. See _agent_finished.
    stopped_early: bool = False


async def run_agent(
    task: str,
    *,
    agent_type: str = "general",
    config: AgentConfig | None = None,
    parent_agent_id: str | None = None,
    session_id: str = "",
    model_id_override: str | None = None,
    context: str = "",
    depth: int = 0,
    agent_name: str | None = None,
    team_id: str | None = None,
    event_channel: str | None = None,
    effort_label: str | None = None,
    structured_schema: dict | None = None,
    isolation: str = "none",
) -> AgentResult:
    """Run an agent with full tool execution loop.

    Args:
        task: The task description for the agent.
        agent_type: Type name from AGENT_TYPES.
        config: Optional custom AgentConfig (overrides agent_type lookup).
        parent_agent_id: ID of spawning agent (for hierarchy tracking).
        session_id: Session scope for tool execution.
        model_id_override: Explicit Bedrock model ID. If None, resolved from config.model.
        context: Additional context prepended to the task.
        depth: Nesting depth (prevents infinite recursion).

    Returns:
        AgentResult with the agent's final output.
    """
    MAX_DEPTH = 5

    if depth >= MAX_DEPTH:
        return AgentResult(
            agent_id="",
            agent_type=agent_type,
            output=f"[Agent Error] Maximum nesting depth ({MAX_DEPTH}) exceeded.",
            status="failed",
        )

    # Resolve config (applies config.json `agent_limits` overrides on top of the
    # built-in AGENT_TYPES preset).
    if config is None:
        config = get_agent_config(agent_type)

    # Resolve model. Agents inherit the session-selected model via
    # model_id_override (threaded from /api/chat through the spawn handlers).
    # With no override, fall back to the user's configured default chat model,
    # never a hardcoded per-agent-type model and never an on-device one (the
    # agent runtime only has cloud provider adapters).
    model_id = _resolve_agent_model(model_id_override, config)
    if not model_id:
        log.warning(
            "Agent %s cannot run: every configured chat model is on-device "
            "(local:*) and the agent runtime has no local adapter.",
            config.agent_type,
        )
        return AgentResult(
            agent_id="",
            agent_type=config.agent_type,
            output=(
                "[Agent Error] No cloud chat model configured: every chat_models "
                "entry is on-device (local:*), which agents cannot use."
            ),
            status="failed",
        )

    # Pre-flight data-retention gate. Mythos-class models (e.g. Fable 5) reject
    # InvokeModel with "data retention mode 'none' is not available for this
    # model" when the account isn't in provider_data_share. Fail ONE agent fast
    # with an actionable message instead of spawning a fan-out of agents that
    # each burn a turn hitting the same ValidationException. Never flips the
    # account setting (that's a consented UI action), and fails open if the mode
    # can't be read.
    from server.infrastructure.data_retention import retention_block_reason

    # The gate can hit the Bedrock CONTROL-PLANE (sync boto3, up to tens of
    # seconds on a degraded endpoint) — run it in the executor like every other
    # boto3 call in this runtime, never directly on the event loop.
    _dr_reason = await asyncio.get_running_loop().run_in_executor(
        _agent_executor, retention_block_reason, model_id
    )
    if _dr_reason:
        log.warning("Agent (%s) blocked by data-retention gate: %s", config.agent_type, _dr_reason)
        return AgentResult(
            agent_id="",
            agent_type=config.agent_type,
            output=f"[Data retention required] {_dr_reason}",
            status="failed",
        )

    # Generate agent ID and register
    agent_id = uuid.uuid4().hex[:10]
    message_bus.create_mailbox(agent_id, session_id=session_id)

    agent_registry.register(
        agent_id,
        config.agent_type,
        task,
        parent_id=parent_agent_id,
        # Record the RESOLVED model, not config.model (an unresolved override
        # that is almost always None = "inherit"). Otherwise every registry
        # entry and progress event reports model=None.
        model=model_id,
        session_id=session_id,
    )

    _ws_token = None
    _wt_session = None
    if isolation == "worktree":
        # git worktree add does a full checkout (seconds on a big repo) — offload
        # it so a fan-out of agents doesn't serialize on the event loop.
        _wt_session = await asyncio.get_running_loop().run_in_executor(
            _git_executor, _enter_agent_worktree, agent_id, session_id
        )
        if _wt_session:
            from server.workspace.state import set_workspace_override

            _ws_token = set_workspace_override(_wt_session.worktree_path)
        elif not config.read_only:
            # Isolation was REQUESTED but could not be created (non-git
            # workspace, transient git error). Never let an agent that was
            # meant to be isolated — possibly a detached one with no human and
            # auto-approved writes — write into the shared workspace instead.
            # Degrade to read-only for this run.
            from dataclasses import replace as _dc_replace

            config = _dc_replace(config, read_only=True)
            log.warning(
                "agent %s requested worktree isolation but none was created; "
                "running read-only to protect the shared workspace",
                agent_id,
            )
    try:
        result = await _run_agent_loop(
            effort_label=effort_label,
            structured_schema=structured_schema,
            agent_id=agent_id,
            task=task,
            context=context,
            config=config,
            model_id=model_id,
            session_id=session_id,
            parent_agent_id=parent_agent_id,
            depth=depth,
            agent_name=agent_name,
            team_id=team_id,
            event_channel=event_channel,
        )
        if _wt_session is not None:
            # Bring the agent's work home: apply its changes UNCOMMITTED to the
            # originating working tree (git status shows them) and remove the
            # worktree + branch. Only a genuinely finished agent's work is
            # applied — a turn-limited/deadline/failed run keeps its worktree
            # for inspection. Clear _wt_session BEFORE awaiting so a harvest
            # failure can't double-harvest via the finally, and guard the await
            # so it can never discard the completed result.
            sess = _wt_session
            _wt_session = None
            try:
                note = await _harvest_worktree(sess, agent_id, _agent_finished(result))
                if note:
                    result.output = (
                        f"{result.output}\n\n[worktree] {note}"
                        if result.output
                        else f"[worktree] {note}"
                    )
                    # Also surface the note as a progress event so it survives
                    # even when result.output is truncated downstream (detached
                    # task result cap) and lands in the detached output file.
                    progress_channel = event_channel or session_id
                    if progress_channel:
                        event_bus.publish(
                            progress_channel,
                            {
                                "agent_id": agent_id,
                                "agent_name": agent_name,
                                "agent_type": config.agent_type,
                                "team_id": team_id,
                                "phase": "text",
                                "text": f"[worktree] {note}",
                            },
                        )
            # BaseException, not Exception: a CancelledError arriving during the
            # post-completion harvest must not throw away the finished result
            # (incl. structured_output for workflow callers).
            except BaseException as e:  # noqa: BLE001 — never lose a finished result
                log.warning("worktree harvest interrupted for %s: %s", agent_id, e)
        agent_registry.update_status(agent_id, result.status, result.output[:500])
        return result
    except asyncio.CancelledError:
        # The agent was stopped (Stop team button, user clicked Stop, or the
        # SSE client disconnected). Mark stopped so list_agents reflects it
        # and tell the UI so the card row flips to the stopped state, then
        # re-raise so the cancellation propagates normally.
        log.info("Agent %s cancelled", agent_id)
        agent_registry.update_status(agent_id, "stopped", "Cancelled by user")
        stop_channel = event_channel or session_id
        if stop_channel:
            event_bus.publish(
                stop_channel,
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "agent_type": config.agent_type,
                    "team_id": team_id,
                    "parent_agent_id": parent_agent_id,
                    "phase": "stopped",
                },
            )
        raise
    except Exception as e:
        log.error("Agent %s failed: %s", agent_id, e, exc_info=True)
        agent_registry.update_status(agent_id, "failed", str(e))
        fail_channel = event_channel or session_id
        if fail_channel:
            event_bus.publish(
                fail_channel,
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "agent_type": config.agent_type,
                    "team_id": team_id,
                    "phase": "failed",
                    "error": str(e),
                },
            )
        return AgentResult(
            agent_id=agent_id,
            agent_type=config.agent_type,
            output=f"[Agent Error] {e}",
            status="failed",
        )
    finally:
        if _ws_token is not None:
            try:
                from server.workspace.state import reset_workspace_override

                reset_workspace_override(_ws_token)
            except Exception:  # noqa: BLE001 — teardown hygiene only
                pass
        if _wt_session is not None:
            # Non-success exit (cancel / unexpected exception): never apply, but
            # tidy a pristine worktree and keep a dirty one for inspection —
            # surfacing WHERE it is via a progress event, since the except
            # branches already built their return value and can't carry the note.
            try:
                note = await _harvest_worktree(_wt_session, agent_id, False)
                progress_channel = event_channel or session_id
                if note and progress_channel:
                    event_bus.publish(
                        progress_channel,
                        {
                            "agent_id": agent_id,
                            "agent_name": agent_name,
                            "agent_type": config.agent_type,
                            "team_id": team_id,
                            "phase": "text",
                            "text": f"[worktree] {note}",
                        },
                    )
            except BaseException:  # noqa: BLE001 — incl. CancelledError mid-teardown
                pass
        message_bus.delete_mailbox(agent_id)
        # Prune stale completed agents once the top-level (coordinator) run
        # finishes consuming results. register()/update_status() are the only
        # things that ever touched the in-memory _agents dict, so without a
        # caller for cleanup_completed() it grew unbounded for the server's
        # lifetime (each entry holds up to 500 chars of result text) — a leak
        # in a long-running server with cron-spawned subagents. cleanup_completed
        # only removes entries older than its age threshold, so it never drops
        # the run that just finished. Gate on depth 0 so nested children don't
        # each re-sweep, and never let teardown break a run.
        if depth == 0:
            try:
                agent_registry.cleanup_completed()
            except Exception as e:
                log.warning("agent_registry.cleanup_completed failed: %s", e)


async def _run_agent_loop(
    *,
    agent_id: str,
    task: str,
    context: str,
    config: AgentConfig,
    model_id: str,
    session_id: str,
    parent_agent_id: str | None,
    depth: int,
    agent_name: str | None = None,
    team_id: str | None = None,
    event_channel: str | None = None,
    effort_label: str | None = None,
    structured_schema: dict | None = None,
) -> AgentResult:
    """Internal agent loop — invoke model with tools until end_turn or limit."""
    from server.chat import assemble_tool_pool
    from server.tool_router import route_tool
    from server.workspace import get_workspace_path

    loop = asyncio.get_event_loop()

    # Build system prompt — include workspace warning if not connected
    ws_path = get_workspace_path()
    ws_note = ""
    if not ws_path and config.agent_type in ("explore", "plan", "verify", "general"):
        ws_note = (
            "\n\nIMPORTANT: No workspace folder is currently open. File tools (ws_read_file, "
            "ws_grep, ws_glob, etc.) are not available. You can only use non-workspace tools. "
            "If the task requires file access, explain that a workspace must be opened first."
        )

    from server.agents.config import AGENT_METHOD

    system = config.system_prompt or (
        "You are a focused subagent. Complete the given task using the available tools. "
        "Be concise and return results directly. "
        "Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen."
        + AGENT_METHOD
    )
    if ws_note:
        system += ws_note

    # Check for pending messages and inject if available
    pending_msgs = message_bus.receive(agent_id)
    msg_context = ""
    if pending_msgs:
        msg_parts = [f"[Message from {m.from_id}]: {m.content}" for m in pending_msgs]
        msg_context = "\n".join(msg_parts) + "\n\n"

    user_content = ""
    if context:
        user_content += f"{context}\n\n"
    if msg_context:
        user_content += msg_context
    user_content += f"Task: {task}"

    messages = [{"role": "user", "content": user_content}]

    # Build filtered tool pool
    all_tools = assemble_tool_pool(plan_mode=False, ws_connected=bool(ws_path))

    # Add agent-specific tools (receive_messages, complete_coordination, and
    # possibly spawn_agent at depth < 4). Deduplicate by name since AGENT_TOOLS
    # already contributes spawn_agent/send_message/list_agents to the main pool.
    from server.agents.tools import get_agent_runtime_tools

    existing_names = {t["name"] for t in all_tools}
    for t in get_agent_runtime_tools(agent_id, depth):
        if t["name"] not in existing_names:
            all_tools.append(t)
            existing_names.add(t["name"])

    tools = filter_tools_for_agent(all_tools, config)

    # Final dedup — MCP servers or plugins may contribute duplicate tool names.
    # Bedrock rejects requests with non-unique tool names.
    seen_names = set()
    deduped_tools = []
    for t in tools:
        if t["name"] not in seen_names:
            seen_names.add(t["name"])
            deduped_tools.append(t)
    tools = deduped_tools

    # Get bedrock client
    from server.agents.providers import TurnUsage, get_adapter, model_key_for_id

    adapter = get_adapter(model_key_for_id(model_id), model_id)
    total_usage = TurnUsage()

    tools_called = []
    collected_text = ""
    all_text_parts: list[str] = []
    # notify_user delivers its message as a route_tool side-effect, not as tool
    # output or model text. A subagent's result is its returned text, so capture
    # notify_user messages and fold them into that result at every exit; else a
    # subagent that "delivers" via notify_user has that content (and its links)
    # silently dropped before the parent ever sees it.
    notifications: list[str] = []

    def _finalize(text: str) -> str:
        """Merge captured notify_user messages with the agent's own text,
        dropping empties and exact dupes (the model may notify AND echo)."""
        blocks: list[str] = []
        seen: set[str] = set()
        for part in [*notifications, text]:
            part = (part or "").strip()
            if part and part not in seen:
                seen.add(part)
                blocks.append(part)
        return "\n\n".join(blocks)

    # Progress events publish to event_channel when set (e.g. a background
    # subagent's private channel) so they don't bleed into a concurrent
    # /api/chat turn draining the session channel; otherwise to session_id.
    progress_channel = event_channel or session_id

    def _emit(phase: str, **extra) -> None:
        """Publish a progress event for this agent (no-op if no channel)."""
        if not progress_channel:
            return
        ev = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "agent_type": config.agent_type,
            "team_id": team_id,
            # Present on spawn_agent children so the team card can badge the
            # row as a spawned child and show who created it.
            "parent_agent_id": parent_agent_id,
            "phase": phase,
            **extra,
        }
        event_bus.publish(progress_channel, ev)

    def _preview(text: str, limit: int = 200) -> str:
        s = (text or "").strip()
        if len(s) <= limit:
            return s
        return s[:limit] + "…"

    # Full task text (generous cap): the card shows the whole brief for rows
    # that were not pre-announced by team_started — spawned children mainly.
    # Report the RESOLVED model_id (threaded into this loop), not config.model
    # which is the unresolved override (almost always None = "inherit").
    _emit("started", task=_preview(task, 2000), model=model_id, max_turns=config.max_turns)

    # Wall-clock deadline is the real "don't loop forever" brake; the turn count
    # is a high backstop. Whichever trips first ends the loop, and either way we
    # finalize gracefully (below) rather than returning a bare stop note.
    deadline = (time.monotonic() + config.deadline_seconds) if config.deadline_seconds else None
    limit_reason = "turn_limit"

    for turn in range(config.max_turns):
        if deadline is not None and time.monotonic() >= deadline:
            limit_reason = "deadline"
            break
        _emit("turn_start", turn=turn + 1)
        # Check for messages between turns
        if turn > 0:
            new_msgs = message_bus.receive(agent_id)
            if new_msgs:
                msg_parts = [f"[Message from {m.from_id}]: {m.content}" for m in new_msgs]
                messages.append({"role": "user", "content": "\n".join(msg_parts)})

        # One model turn through the provider adapter (Anthropic or OpenAI
        # on Bedrock) — the rest of this loop is provider-neutral.
        turn_result = await adapter.invoke(
            system=system,
            messages=messages,
            tools=tools or None,
            max_tokens=config.max_tokens,
            effort_label=effort_label,
        )
        total_usage.add(turn_result.usage)
        stop_reason = turn_result.stop_reason
        tool_uses = turn_result.tool_calls
        result_content = turn_result.assistant_blocks

        if turn_result.text:
            all_text_parts.append(turn_result.text)
            if turn_result.text.strip():
                _emit("text", turn=turn + 1, text=_preview(turn_result.text, 2000))
        collected_text = "\n\n".join(all_text_parts)

        if stop_reason != "tool_use" or not tool_uses:
            structured = None
            if structured_schema is not None:
                messages.append(
                    {
                        "role": "assistant",
                        "content": result_content
                        or [{"type": "text", "text": collected_text or "(done)"}],
                    }
                )
                structured = await _distill_structured(
                    adapter, system, messages, structured_schema, config, total_usage
                )
            _emit("completed", turn=turn + 1, turns_used=turn + 1, status="completed")
            return AgentResult(
                agent_id=agent_id,
                agent_type=config.agent_type,
                output=_finalize(collected_text),
                status="completed",
                turns_used=turn + 1,
                tools_called=tools_called,
                usage=total_usage.as_dict(),
                structured_output=structured,
            )

        # Execute tool calls
        messages.append({"role": "assistant", "content": result_content})
        tool_results = []

        for tu in tool_uses:
            tool_name = tu["name"]
            tool_input = tu.get("input", {})
            # Build the executor input as a COPY before injecting the internal
            # session id, so tu["input"] (replayed in the assistant message
            # every turn) never carries __session_id__ into the transcript.
            call_input = _with_session_id(tool_input, session_id)
            tools_called.append(tool_name)

            _visible_input = json.dumps(
                {k: v for k, v in call_input.items() if not str(k).startswith("__")}
            )
            _emit(
                "tool_call",
                turn=turn + 1,
                tool_name=tool_name,
                # Short preview fills the collapsed log line; the full input
                # (capped) feeds the click-to-expand view.
                tool_input_preview=_preview(_visible_input, 240),
                tool_input_full=_preview(_visible_input, 4000),
            )

            # PreToolUse gate — blocking hooks apply to subagents too, so a
            # policy that denies (e.g.) ws_run_command holds inside spawned
            # agents, not just the main chat loop.
            _pre = await run_hooks(
                "PreToolUse",
                {
                    "event": "PreToolUse",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "session_id": session_id,
                    "model_id": model_id,
                },
                tool_name=tool_name,
            )
            if _pre.blocked:
                _denied = f"[Hook denied] {_pre.reason}"
                _emit(
                    "tool_result",
                    turn=turn + 1,
                    tool_name=tool_name,
                    output_preview=_preview(_denied, 240),
                    output_full=_preview(_denied, 4000),
                    status="error",
                )
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tu["id"], "content": _denied}
                )
                continue
            if _pre.updated_input is not None:
                call_input = _with_session_id(_pre.updated_input, session_id)

            try:
                # Handle agent-specific tools inline
                if tool_name == "spawn_agent":
                    output = await _handle_spawn_agent(
                        call_input,
                        agent_id,
                        session_id,
                        model_id,
                        depth,
                        team_id=team_id,
                        event_channel=event_channel,
                        effort_label=effort_label,
                    )
                elif tool_name == "send_message":
                    output = _handle_send_message(call_input, agent_id)
                elif tool_name == "list_agents":
                    output = _handle_list_agents(session_id)
                elif tool_name == "receive_messages":
                    output = _handle_receive_messages(agent_id)
                elif tool_name == "complete_coordination":
                    output = call_input.get("summary", "Coordination complete.")
                    # Return immediately — coordinator is done
                    _emit("completed", turn=turn + 1, turns_used=turn + 1, status="completed")
                    return AgentResult(
                        agent_id=agent_id,
                        agent_type=config.agent_type,
                        output=_finalize(output),
                        status="completed",
                        turns_used=turn + 1,
                        tools_called=tools_called,
                        usage=total_usage.as_dict(),
                    )
                else:
                    # Route through standard tool router
                    output, side_effects = await route_tool(
                        tool_name,
                        call_input,
                        loop=loop,
                        executor=_agent_executor,
                        transcript="",
                        attachments=None,
                        session_id=session_id,
                        model_id=model_id,
                        tool_use_id=tu["id"],
                        origin="agent",
                    )
                    # Capture notify_user content so it reaches the parent via
                    # this agent's returned output (see _finalize).
                    for se in side_effects:
                        if "notify_user" in se:
                            msg = (se["notify_user"] or {}).get("message", "")
                            if msg:
                                notifications.append(msg)
                    # Handle WS_APPROVAL responses — auto-approve in agent context.
                    # agent=True stamps the payload so high-blast-radius executors
                    # (e.g. GitHub mutations) can refuse to run with no human present.
                    if isinstance(output, str) and output.startswith("[WS_APPROVAL]"):
                        from server.tool_executor import _execute_ws_approval_inline

                        ws_data = output[len("[WS_APPROVAL]") :]
                        ws_parsed = json.loads(ws_data)
                        output = await _execute_ws_approval_inline(ws_parsed, agent=True)
                    # Workspace-prompt cannot be resolved inside a subagent
                    # (no user present) — tell the agent so it can adapt.
                    elif isinstance(output, str) and output.startswith("[WS_WORKSPACE_PROMPT]"):
                        output = (
                            "Error: no workspace connected and cannot prompt "
                            "the user from a subagent. Ask the parent to "
                            "open a workspace first."
                        )

            except Exception as e:
                log.error("Agent %s tool error (%s): %s", agent_id, tool_name, e)
                output = f"[Tool Error] {e}"

            output_str = str(output)
            _emit(
                "tool_result",
                turn=turn + 1,
                tool_name=tool_name,
                output_preview=_preview(output_str, 240),
                output_full=_preview(output_str, 4000),
                status="error" if output_str.startswith("[Tool Error]") else "ok",
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": output_str[:50000],
                }
            )

        messages.append({"role": "user", "content": tool_results})

    # Loop ended without a natural finish — either the turn backstop was
    # exhausted or the wall-clock deadline tripped. Finalize gracefully so the
    # run still yields a usable result: distill structured output for schema
    # callers (otherwise the turn-limit path left structured_output=None, which
    # surfaces to workflow scripts as a null agent() result) and, when the agent
    # spent every turn on tools without emitting a final answer, do one no-tools
    # pass to extract its best effort.
    turns_used = (turn + 1) if limit_reason == "turn_limit" else turn
    reason_text = (
        "reached time limit"
        if limit_reason == "deadline"
        else f"reached turn limit ({config.max_turns})"
    )

    structured = None
    if structured_schema is not None:
        structured = await _distill_structured(
            adapter, system, messages, structured_schema, config, total_usage
        )

    body_text = _finalize(collected_text)
    if structured is None and not body_text.strip():
        try:
            fin = await adapter.invoke(
                system=system,
                messages=[
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "You are out of budget and cannot call any more tools. "
                            "Give your best complete final answer now, based on what "
                            "you have gathered so far."
                        ),
                    },
                ],
                # The transcript contains tool_use/tool_result blocks, and the
                # Anthropic API rejects such requests unless the tools param is
                # present — tools=None here made this finalize pass silently
                # fail (ValidationException, caught below) on every Anthropic
                # model. Keep the tool definitions; the prompt forbids calls.
                tools=tools or None,
                max_tokens=config.max_tokens,
                effort_label=None,
            )
            total_usage.add(fin.usage)
            if fin.text and fin.text.strip():
                body_text = _finalize(fin.text)
        except Exception as e:  # noqa: BLE001 — best-effort finalize, never fatal
            log.debug("Agent %s finalize pass failed: %s", agent_id, e)

    stop_note = f"[Agent stopped - {reason_text}]"
    final_output = f"{stop_note}\n\n{body_text}" if body_text.strip() else stop_note
    # Keep status="completed" for backward compatibility (memory/subagent callers
    # branch on it); the emitted `turn_limit` phase is what drives the UI badge.
    _emit("turn_limit", turns_used=turns_used, status="turn_limit")
    return AgentResult(
        agent_id=agent_id,
        agent_type=config.agent_type,
        output=final_output,
        status="completed",
        turns_used=turns_used,
        tools_called=tools_called,
        usage=total_usage.as_dict(),
        structured_output=structured,
        # Signal a limit exit (not a clean finish) so the worktree harvest keeps
        # the partial work for inspection instead of applying it.
        stopped_early=True,
    )


def _enter_agent_worktree(agent_id: str, session_id: str):
    """Create (or resume) an isolated git worktree for this agent.

    Only when the global workspace is a git repo; failures degrade to the
    shared workspace with a warning (isolation is an optimization for
    parallel writes, not a correctness gate). Returns the WorktreeSession so
    run_agent can HARVEST it on completion: the agent's changes are applied
    uncommitted back to the originating working tree and the worktree +
    branch are removed (see server/git/worktree_harvest.py).
    """
    try:
        import os as _os

        from server.git.worktree_session import enter_worktree
        from server.workspace.state import get_workspace_path

        repo_root = get_workspace_path()
        if not repo_root or not _os.path.exists(_os.path.join(repo_root, ".git")):
            return None
        # Namespaced session key: enter_worktree records a session->worktree
        # mapping meant for CHAT sessions; an agent must never clobber the
        # user's own worktree state for the real session id.
        return enter_worktree(repo_root, f"agent-{agent_id}", f"agent:{agent_id}")
    except Exception as e:
        log.warning("agent worktree isolation failed (%s); using shared workspace", e)
        return None


def _agent_finished(result: "AgentResult") -> bool:
    """True only when the agent genuinely completed its goal — so its worktree
    work is applied. A turn/deadline limit reports status='completed' (kept that
    way for memory/subagent callers) but sets stopped_early, meaning the work is
    partial and the worktree is kept for inspection instead of applied."""
    return result.status == "completed" and not result.stopped_early


async def _harvest_worktree(wt_session, agent_id: str, apply_changes: bool) -> str:
    """Run the worktree harvest off-loop (dedicated git pool) and return its
    user-facing note."""
    from functools import partial

    from server.git.worktree_harvest import harvest_agent_worktree

    call = partial(
        harvest_agent_worktree,
        wt_session.original_cwd,
        wt_session.worktree_path,
        wt_session.worktree_branch,
        f"agent:{agent_id}",
        apply_changes,
        base_commit=wt_session.original_head_commit,
    )
    outcome = await asyncio.get_running_loop().run_in_executor(_git_executor, call)
    return outcome.get("note", "")


async def _distill_structured(adapter, system, messages, schema, config, total_usage):
    """One forced-structured call over the finished transcript, with a single
    schema-repair retry. jsonschema is a hard dependency of the venv (via mcp)
    but validation failing twice returns None rather than raising — callers
    decide whether an unstructured fallback is acceptable."""
    from server.agents.providers.base import TurnUsage as _TU

    ask = {
        "role": "user",
        "content": (
            "Now emit the final structured result for the task above using the "
            "emit_result tool (or the required JSON format). Output the complete "
            "object only."
        ),
    }
    attempt_messages = [*messages, ask]
    for attempt in range(2):
        turn = await adapter.invoke(
            system=system,
            messages=attempt_messages,
            tools=None,
            max_tokens=config.max_tokens,
            effort_label=None,
            force_structured=schema,
        )
        if isinstance(turn.usage, _TU):
            total_usage.add(turn.usage)
        candidate = turn.structured_output
        if candidate is not None:
            try:
                import jsonschema

                jsonschema.validate(candidate, schema)
                return candidate
            except Exception as e:
                if attempt == 0:
                    attempt_messages = [
                        *attempt_messages,
                        {"role": "assistant", "content": json.dumps(candidate)},
                        {
                            "role": "user",
                            "content": f"That did not validate against the schema ({e}). "
                            "Emit a corrected complete object.",
                        },
                    ]
                    continue
                log.warning("structured output failed validation twice: %s", e)
                return None
        if attempt == 0:
            continue
    return None


# --- Inline handlers for agent-specific tools ---


async def _handle_spawn_agent(
    tool_input: dict,
    parent_id: str,
    session_id: str,
    model_id: str,
    depth: int,
    *,
    team_id: str | None = None,
    event_channel: str | None = None,
    effort_label: str | None = None,
) -> str:
    """Handle spawn_agent tool call from within an agent."""
    child_task = tool_input.get("task", "")
    child_type = tool_input.get("agent_type", "general")
    child_context = tool_input.get("context", "")

    result = await run_agent(
        child_task,
        agent_type=child_type,
        effort_label=effort_label,
        parent_agent_id=parent_id,
        session_id=session_id,
        context=child_context,
        depth=depth + 1,
        # Child inherits the parent's (session) model rather than its
        # agent-type default, so the whole tree uses one model.
        model_id_override=model_id,
        # Thread the parent's team and event channel through so the child's
        # progress events land in the SAME team card as its own row (keyed by
        # agent_id, badged as a child) instead of being dropped for lacking
        # a team_id. This is what makes "agents spawning agents" visible.
        team_id=team_id,
        event_channel=event_channel,
    )

    return (
        f"[Agent {result.agent_id} ({result.agent_type})] "
        f"Status: {result.status}, Turns: {result.turns_used}\n"
        f"Output:\n{result.output}"
    )


def _handle_send_message(tool_input: dict, from_id: str) -> str:
    """Handle send_message tool call."""
    to_id = tool_input.get("to_agent_id", "")
    content = tool_input.get("content", "")
    is_broadcast = tool_input.get("broadcast", False)

    if is_broadcast:
        count = message_bus.broadcast(from_id, content)
        return f"Broadcast sent to {count} agents."
    elif to_id:
        message_bus.send(from_id, to_id, content)
        return f"Message sent to agent {to_id}."
    else:
        return "Error: specify to_agent_id or set broadcast=true."


def _handle_list_agents(session_id: str) -> str:
    """Handle list_agents tool call."""
    agents = agent_registry.list_all(session_id)
    if not agents:
        return "No active agents."
    lines = []
    for a in agents:
        lines.append(f"- {a.agent_id} ({a.agent_type}): {a.status} - {a.task[:100]}")
    return "\n".join(lines)


def _handle_receive_messages(agent_id: str) -> str:
    """Handle receive_messages tool call."""
    msgs = message_bus.receive(agent_id)
    if not msgs:
        return "No pending messages."
    lines = [f"[From {m.from_id}]: {m.content}" for m in msgs]
    return "\n".join(lines)
