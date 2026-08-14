"""
Agent runtime — executes an agent with a full tool loop.

Unlike the main chat stream (SSE, streaming), agent runtime uses
non-streaming invoke_model calls in a loop until the agent produces
a final text response or hits its turn limit.
"""

import asyncio
import contextvars
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
from server.agents.providers.base import AGENT_CALL_CONCURRENCY
from server.agents.registry import agent_registry

log = logging.getLogger("whisper-studio")

# Shared executor for agent bedrock calls. Sized from the ONE concurrency
# knob (server/agents/providers/base.py) rather than its own literal: this
# pool is what actually gates how many agent model calls can be in flight on
# the chat/engine path, so a smaller number here silently capped the
# workflow runtime's 16-way fan-out no matter what the scheduler admitted.
_agent_executor = ThreadPoolExecutor(max_workers=AGENT_CALL_CONCURRENCY)
# Separate small pool for git/worktree harvesting so a burst of finishing
# agents doing git ops can't head-of-line-block model calls on _agent_executor.
_git_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-git")

# Ambient nesting context for a spawn_agent/send_message/receive_messages/
# complete_coordination tool call dispatched from INSIDE this unattended
# loop, as opposed to interactive chat. server.tool_router.route_tool reads
# this so a nested call threads the CALLING agent's real id/depth/team/event-
# channel through (server/agent_tools/spawn.py) instead of behaving like a
# fresh, disconnected top-level spawn every time — that would both lose the
# MAX_DEPTH recursion guard (every route_tool call defaults to depth=0) and
# stop nested progress from nesting under the parent's own team card.
# None outside an agent's own loop. Set via .set()/.reset() bracketing the
# run_turn() drain in _run_agent_loop: contextvars copy the AMBIENT context
# at asyncio.create_task() time, so this propagates into the task run_turn
# creates for execute_tool_batch (and from there into a further nested
# run_agent's own task), but a sibling task never sees it.
agent_nesting_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "agent_nesting_ctx", default=None
)


def _with_session_id(tool_input: dict, session_id: str) -> dict:
    """Return a COPY of the model's tool input with internal markers injected
    for the executor: the session id, and an unattended-agent stamp.

    The original ``tool_input`` is ``tu["input"]`` — the exact dict that lives
    inside the assistant message replayed to Bedrock on every subsequent turn.
    Mutating it in place would leak the internal ``__session_id__``/``__agent__``
    keys into the transcript (where the model can see and imitate them), and
    some executors only ``.get()`` rather than ``.pop()`` them, so they'd
    persist. Copying keeps ``tu["input"]`` pristine, mirroring the main chat
    path (tool_executor.py).

    ``__agent__`` marks every tool call dispatched from this unattended loop —
    no human is present to answer on-the-spot questions. High-blast-radius
    executors (github mutations via refuse_if_agent, and MCP's elicitation
    callback) check for this stamp and refuse/auto-decline rather than acting
    or answering on a human's behalf.
    """
    call_input = dict(tool_input)
    call_input["__session_id__"] = session_id
    call_input["__agent__"] = True
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
    workspace_path: str | None = None,
) -> AgentResult:
    """Run an agent with full tool execution loop.

    Args:
        task: The task description for the agent.
        agent_type: Type name from AGENT_TYPES.
        config: Optional custom AgentConfig (overrides agent_type lookup).
        parent_agent_id: ID of spawning agent (for hierarchy tracking).
        session_id: Session scope for tool execution.
        model_id_override: Explicit Bedrock model ID. If None, resolved from the config default.
        context: Additional context prepended to the task.
        depth: Nesting depth (prevents infinite recursion).
        workspace_path: Pin this agent's workspace to an explicit root instead
            of whatever is globally connected — a workflow run's frozen
            launch-time workspace (server.workflows.manager.
            resolve_workflow_workspace). Ignored outside that caller; None
            preserves the ambient global-workspace behavior every other
            caller already gets.

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
        # Record the RESOLVED model so every registry entry and progress
        # event carries a real model id.
        model=model_id,
        session_id=session_id,
    )

    def _pin_plain_override(path: str):
        # Mark the override live BEFORE any tool call can dispatch a command
        # against it, so a command that outlives this agent's own cancellation
        # (still running in a worker thread when the finally below retires the
        # override) gets refused instead of resurrecting a cwd entry nothing
        # will ever clear again.
        from server.cwd_tracker import register_override
        from server.workspace.state import set_workspace_override

        tok = set_workspace_override(path)
        register_override(path)
        return tok

    _ws_token = None
    _wt_session = None
    _override_path = None
    if isolation == "worktree":
        # git worktree add does a full checkout (seconds on a big repo) — offload
        # it so a fan-out of agents doesn't serialize on the event loop. Fork
        # from the caller's pinned root when it gave one (a workflow run),
        # else whatever is globally connected (loop.run_in_executor does NOT
        # propagate contextvars, so the ambient override wouldn't be visible
        # here anyway — repo_root is how a pin actually reaches this thread).
        _wt_session = await asyncio.get_running_loop().run_in_executor(
            _git_executor, _enter_agent_worktree, agent_id, session_id, workspace_path
        )
        if _wt_session:
            _override_path = _wt_session.worktree_path
            _ws_token = _pin_plain_override(_override_path)
        else:
            if not config.read_only:
                # Isolation was REQUESTED but could not be created (non-git
                # workspace, transient git error). Never let an agent that was
                # meant to be isolated — possibly a detached one with no human
                # and auto-approved writes — write into the shared workspace
                # instead. Degrade to read-only for this run.
                from dataclasses import replace as _dc_replace

                config = _dc_replace(config, read_only=True)
                log.warning(
                    "agent %s requested worktree isolation but none was created; "
                    "running read-only to protect the shared workspace",
                    agent_id,
                )
            if workspace_path:
                # Isolation degraded (or was already unavailable), but a
                # pinned root still exists — this agent must still resolve
                # every workspace tool against IT, not whatever is globally
                # connected. Without this fallback, a workflow pinned to a
                # plain (non-git) folder silently lost its pin the moment a
                # script asked for isolation:"worktree".
                _override_path = workspace_path
                _ws_token = _pin_plain_override(_override_path)
    elif workspace_path:
        # No isolation requested, but the caller has a pinned root (a workflow
        # run without isolation:"worktree") — this agent must still resolve
        # every workspace tool against THAT root, not whatever the global
        # connection happens to be by the time this particular agent starts.
        _override_path = workspace_path
        _ws_token = _pin_plain_override(_override_path)
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
        if _override_path:
            try:
                from server.cwd_tracker import clear_override

                clear_override(session_id, _override_path)
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
    """Internal agent loop — the shared chat/engine turn loop
    (server.chat.engine.runner.run_turn) instead of a hand-rolled round loop.

    Ports agents onto the exact engine interactive chat uses: round budgets,
    the salvage round, the shared tool-execution safety gate (execute_tool_batch
    + process_tool_results — every tool call flows through server.tool_router,
    same as chat), and unattended resolution of approval/pause-inducing tools
    (TurnContext.unattended). completion_gate is forced OFF — an agent must
    never trigger the PARENT chat turn's goal-gate evaluator — and
    turn_scope_id keys this run's goal-store/auto-mode-breaker/paused-turn
    state so it can never collide with a concurrently running parent turn
    that shares the same session_id (e.g. the chat turn whose spawn_agent
    call is still awaiting this very run).

    Not migrated, by design: _distill_structured (a one-shot forced-tool
    call over the finished transcript; chat/engine's adapters have no
    equivalent yet) still goes through server.agents.providers' own adapter
    system, unchanged. _resolve_agent_model's local:* guard is untouched.
    """
    from server.attachment_store import load_session_attachments
    from server.chat.tool_activation import activate_from_history
    from server.chat.tool_index import build_deferred_index
    from server.chat.tool_pool import assemble_partitioned_pool
    from server.workspace import get_workspace_path

    loop = asyncio.get_event_loop()

    # Subagents inherit the parent session's durable attachments (documents,
    # images) so analyze_document etc. work the same as they do in interactive
    # chat — same lookup routes.py uses, just keyed off the same session_id
    # spawn_agent already threads through for messaging/progress routing.
    current_attachments = (
        await asyncio.to_thread(load_session_attachments, session_id) if session_id else {}
    )

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

    # Progressive tool disclosure: re-derive this session's activations from
    # visible history first (self-healing across restarts, exactly like
    # server/chat/routes.py), then build the filtered tool pool ONCE for the
    # whole run — identical logic to the pre-migration loop (assemble_tool_pool
    # + agent-runtime tools + filter_tools_for_agent + final dedup), just fed
    # from the core+activated set instead of the full catalog — and hand it to
    # the engine as a precomputed TurnContext.tool_catalog instead of letting
    # it reassemble a chat-shaped catalog every round. Everything deferred is
    # folded into a compact index appended to the system prompt below.
    activate_from_history(session_id, messages)
    all_tools, _deferred, _core_count = assemble_partitioned_pool(
        plan_mode=False, ws_connected=bool(ws_path), session_id=session_id
    )
    deferred_tool_index = build_deferred_index(_deferred)
    if deferred_tool_index:
        system += "\n\n" + deferred_tool_index

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

    def _tool_catalog() -> tuple[list[dict], int | None]:
        return tools, None

    # Provider adapter selection — the same chat_model_meta.provider check
    # server/chat/routes.py uses to choose between the two chat/engine
    # adapters for interactive chat.
    from server.agents.providers import model_key_for_id
    from server.chat.infra import _get_chat_model_meta

    model_key = model_key_for_id(model_id)
    _agent_meta = _get_chat_model_meta().get(model_key, {})
    _provider = _agent_meta.get("provider", "anthropic")

    # The parent's effort was clamped to the PARENT's model. An agent can run on
    # a different one (opts.model, a config default, a per-agent-type pin), so
    # re-resolve against this model rather than forwarding a label it may not
    # support: an ultracode parent used to hand a Sonnet child a raw reasoning
    # value from Opus's ladder. resolve_effort keeps the level where it is when
    # the model does support it, drops to the nearest lower rung when it does
    # not, and returns None for models with no effort at all (Haiku).
    # This value rides ctx.effort_label too, so agents nested below this one
    # inherit the same level instead of restarting from a default.
    # Skipped for OpenAI children: reasoning_effort_for already maps EVERY app
    # label to that model's ladder itself, including "extra", which the openai
    # tier list deliberately omits because the GPT picker does not offer it.
    # Clamping against that list would read the omission as "unsupported" and
    # demote an extra-effort parent's GPT child from xhigh to high.
    from server.infrastructure.effort import resolve_effort

    if effort_label is not None and _provider != "openai_bedrock":
        effort_label = resolve_effort(_agent_meta, model_key, effort_label)
    if _provider == "openai_bedrock":
        from server.chat.engine.openai import OpenAIResponsesAdapter

        adapter = OpenAIResponsesAdapter(
            model_key=model_key,
            model_id=model_id,
            system_prompt=system,
            effort_label=effort_label,
            session_id=session_id,
        )
    else:
        from server.chat.engine.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(
            model_key=model_key,
            model_id=model_id,
            system_prompt=system,
            # Agents don't use prompt-cache checkpoints (the pre-migration
            # loop never did either) — system_static/system_dynamic only
            # matter when caching_on is True.
            system_static="",
            system_dynamic="",
            caching_on=False,
            cache_ttl="5m",
            effort_label=effort_label,
            force_skill=None,
            loop=loop,
            executor=_agent_executor,
        )

    tools_called: list[str] = []
    all_text_parts: list[str] = []
    # notify_user delivers its message as a route_tool side-effect, not as tool
    # output or model text. A subagent's result is its returned text, so capture
    # notify_user messages and fold them into that result at every exit; else a
    # subagent that "delivers" via notify_user has that content (and its links)
    # silently dropped before the parent ever sees it.
    notifications: list[str] = []
    # complete_coordination is now a normal tool call (server/tool_router.py)
    # rather than a hard mid-round stop — capture its summary so it becomes
    # the coordinator's definitive output, same as the pre-migration inline
    # handler's immediate return did.
    coordination_summary: str | None = None

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
        return s[:limit] + "\u2026"

    # Full task text (generous cap): the card shows the whole brief for rows
    # that were not pre-announced by team_started — spawned children mainly.
    # Report the RESOLVED model_id (threaded into this loop).
    _emit("started", task=_preview(task, 2000), model=model_id, max_turns=config.max_turns)

    from server.chat.engine.policy import TurnPolicy
    from server.chat.engine.runner import TurnContext, run_turn

    ctx = TurnContext(
        session_id=session_id,
        model_key=model_key,
        model_id=model_id,
        messages=messages,
        adapter=adapter,
        policy=TurnPolicy(
            max_rounds=config.max_turns,
            deadline_seconds=config.deadline_seconds,
            # Agents must never trigger the parent chat turn's goal-gate
            # evaluator.
            completion_gate=False,
            salvage_round=True,
        ),
        loop=loop,
        executor=_agent_executor,
        current_attachments=current_attachments,
        tool_exec_model_id=model_id,
        effort_label=effort_label,
        # Agents don't run interactive chat's post-turn memory hooks (auto
        # memory / session memory / dream consolidation) — the pre-migration
        # loop never fired them either.
        memory_hooks=lambda msgs: None,
        unattended=True,
        turn_scope_id=f"agent:{agent_id}",
        tool_catalog=_tool_catalog,
    )

    # Drain run_turn: an async generator yielding SSE-format ndjson strings
    # ("data: {...}\n\n" / "data: [DONE]\n\n" / ": hb\n\n" heartbeat
    # comments). THIS PARSING IS COUPLED TO runner.py's PINNED SSE FRAME
    # VOCABULARY (see its own module docstring on golden-fixture pinning) —
    # a future frame rename/reshape needs a matching update here. Kept
    # deliberately minimal: text (buffered per round, flushed on the round's
    # usage frame — mirroring the pre-migration loop's one "text" event per
    # turn, not one per streamed delta), the running usage total, tool call/
    # result previews for progress events, and notify_user/complete_coordination
    # payloads folded into the returned output.
    turn_start = time.monotonic()
    rounds_used = 0
    turn_no = 0
    final_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    round_text_parts: list[str] = []
    error_text: str | None = None

    def _flush_round_text() -> None:
        if not round_text_parts:
            return
        round_text = "".join(round_text_parts)
        round_text_parts.clear()
        if round_text.strip():
            all_text_parts.append(round_text)
            _emit("text", turn=turn_no, text=_preview(round_text, 2000))

    _nesting_token = agent_nesting_ctx.set(
        {
            "agent_id": agent_id,
            "depth": depth,
            "team_id": team_id,
            "event_channel": event_channel,
        }
    )
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
                    # One usage frame per round, with CUMULATIVE totals — take
                    # the latest as the running total, and flush this round's
                    # buffered text (all of it already streamed by this point;
                    # runner.py emits usage right after the round's content).
                    u = frame["usage"]
                    final_usage = {
                        "input_tokens": u.get("total_input", 0),
                        "output_tokens": u.get("total_output", 0),
                        "cache_read_tokens": u.get("total_cache_read", 0),
                        "cache_creation_tokens": u.get("total_cache_creation", 0),
                    }
                    rounds_used += 1
                    turn_no = rounds_used
                    _flush_round_text()
                    _emit("turn_start", turn=turn_no + 1)
                elif "text" in frame and isinstance(frame.get("text"), str):
                    round_text_parts.append(frame["text"])
                elif "error" in frame:
                    error_text = frame["error"]
                elif "skill_input" in frame:
                    tool_name = frame["skill_input"]
                    tool_input_visible = frame.get("input") or {}
                    tools_called.append(tool_name)
                    if tool_name == "complete_coordination":
                        coordination_summary = (
                            tool_input_visible.get("summary") or "Coordination complete."
                        )
                    _visible_input = json.dumps(tool_input_visible)
                    _emit(
                        "tool_call",
                        turn=turn_no,
                        tool_name=tool_name,
                        tool_input_preview=_preview(_visible_input, 240),
                        tool_input_full=_preview(_visible_input, 4000),
                    )
                elif "skill_result" in frame:
                    tool_name = frame["skill_result"]
                    output_preview = frame.get("output") or ""
                    _emit(
                        "tool_result",
                        turn=turn_no,
                        tool_name=tool_name,
                        output_preview=_preview(output_preview, 240),
                        output_full=_preview(output_preview, 4000),
                        status="error" if output_preview.startswith("[Tool Error]") else "ok",
                    )
                elif "notify_user" in frame:
                    msg = (frame["notify_user"] or {}).get("message", "")
                    if msg:
                        notifications.append(msg)
    finally:
        agent_nesting_ctx.reset(_nesting_token)
        _flush_round_text()  # catch trailing text with no following usage frame

    elapsed = time.monotonic() - turn_start
    # run_turn's own last-round handling (the max_rounds cap, or a deadline
    # hit — both force tools off for that round, see server/chat/engine/
    # runner.py) already produces a genuine best-effort final answer; there is
    # no separate "out of budget, finalize now" call to make here, unlike the
    # pre-migration loop. stopped_early can't be read directly off the drained
    # frames (no frame says "this was the capped round"), so it is inferred
    # exactly the way the pre-migration loop's own limit detection worked:
    # the round budget was exhausted, or the wall-clock deadline passed.
    stopped_early = rounds_used >= config.max_turns or (
        config.deadline_seconds is not None and elapsed >= config.deadline_seconds
    )

    collected_text = "\n\n".join(all_text_parts)
    body_text = _finalize(
        coordination_summary if coordination_summary is not None else collected_text
    )

    structured = None
    if structured_schema is not None:
        # Kept on the OLD provider-adapter system (server.agents.providers) —
        # _distill_structured is a one-shot, non-agentic forced-tool call the
        # chat/engine adapters have no equivalent for yet. Out of scope for
        # this migration (see the module docstring above).
        from server.agents.providers import TurnUsage as _OldTurnUsage
        from server.agents.providers import get_adapter as _get_old_adapter

        _old_adapter = _get_old_adapter(model_key, model_id)
        _old_usage = _OldTurnUsage(
            input_tokens=final_usage["input_tokens"],
            output_tokens=final_usage["output_tokens"],
            cache_read_tokens=final_usage["cache_read_tokens"],
            cache_creation_tokens=final_usage["cache_creation_tokens"],
        )
        if stopped_early:
            # Matches the pre-migration tail path: no synthetic assistant
            # turn appended, ctx.messages already ends with the last tool
            # result.
            _distill_messages = ctx.messages
        else:
            # Matches the pre-migration natural-finish path: append the
            # final round's own answer as one more assistant turn first.
            _distill_messages = [
                *ctx.messages,
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": collected_text or "(done)"}],
                },
            ]
        structured = await _distill_structured(
            _old_adapter, system, _distill_messages, structured_schema, config, _old_usage
        )
        final_usage = _old_usage.as_dict()

    if stopped_early:
        reason_text = (
            "reached time limit"
            if config.deadline_seconds is not None and elapsed >= config.deadline_seconds
            else f"reached turn limit ({config.max_turns})"
        )
        stop_note = f"[Agent stopped - {reason_text}]"
        final_output = f"{stop_note}\n\n{body_text}" if body_text.strip() else stop_note
        # Keep status="completed" for backward compatibility (memory/subagent
        # callers branch on it); the emitted turn_limit phase drives the UI
        # badge, and stopped_early is what gates the worktree harvest.
        _emit("turn_limit", turns_used=rounds_used, status="turn_limit")
        return AgentResult(
            agent_id=agent_id,
            agent_type=config.agent_type,
            output=final_output,
            status="completed",
            turns_used=rounds_used,
            tools_called=tools_called,
            usage=final_usage,
            structured_output=structured,
            stopped_early=True,
        )

    if error_text and not body_text.strip():
        # A terminal round error with nothing else produced — surface it
        # plainly instead of an empty result.
        body_text = f"[Agent Error] {error_text}"

    _emit("completed", turn=rounds_used, turns_used=rounds_used, status="completed")
    return AgentResult(
        agent_id=agent_id,
        agent_type=config.agent_type,
        output=body_text,
        status="completed",
        turns_used=rounds_used,
        tools_called=tools_called,
        usage=final_usage,
        structured_output=structured,
    )


def _enter_agent_worktree(agent_id: str, session_id: str, repo_root: str | None = None):
    """Create (or resume) an isolated git worktree for this agent.

    Only when the target is a git repo; failures degrade to the shared
    workspace with a warning (isolation is an optimization for parallel
    writes, not a correctness gate). Returns the WorktreeSession so run_agent
    can HARVEST it on completion: the agent's changes are applied uncommitted
    back to the originating working tree and the worktree + branch are
    removed (see server/git/worktree_harvest.py).

    ``repo_root`` lets a caller with its own pinned root (a workflow run) fork
    from that instead of whatever is globally connected. This runs on a
    worker thread via loop.run_in_executor, which does NOT propagate
    contextvars, so a caller cannot rely on set_workspace_override being
    visible here — it must pass the root explicitly.
    """
    try:
        import os as _os

        from server.git.worktree_session import enter_worktree
        from server.workspace.state import get_workspace_path

        repo_root = repo_root or get_workspace_path()
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
