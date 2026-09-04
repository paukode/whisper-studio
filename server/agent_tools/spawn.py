"""Single-agent orchestration: spawn (blocking or detached), inter-agent
messaging, and the active-agent listing. Cost rollup lives here too since both
spawn and team runs feed it.
"""

import json
import logging
import uuid

log = logging.getLogger("whisper-studio")

# Max concurrently-RUNNING detached agents per session: fire-and-forget
# spawns must not starve the shared model-call throttle interactive
# agents use, nor pile up unbounded background work.
DETACHED_PER_SESSION_CAP = 8


def _spawn_label(task: str, max_len: int = 60) -> str:
    """A short, single-line title for a spawned agent's card, from its task."""
    first = (task or "").strip().splitlines()[0].strip() if task and task.strip() else "agent"
    return first if len(first) <= max_len else first[: max_len - 1].rstrip() + "…"


def _start_detached_from_tool(
    task: str,
    agent_type: str,
    context: str,
    session_id: str,
    model_id: str | None,
    effort_label: str | None,
    isolation: str = "none",
) -> str:
    from server.tasks import registry
    from server.tasks.agents import start_detached_agent

    running = registry.list_tasks(session_id=session_id, status="running", kind="agent")
    if len(running) >= DETACHED_PER_SESSION_CAP:
        return json.dumps(
            {
                "error": (
                    f"detached-agent cap reached ({DETACHED_PER_SESSION_CAP} running). "
                    "Wait for completions (task_status) or stop one (task_cancel)."
                )
            }
        )
    full_task = f"{context}\n\n{task}".strip() if context else task
    # SAFETY DEFAULT: a detached run has no human present, and agents
    # auto-approve the [WS_APPROVAL] write gate — so detached agents run
    # under the read-only tool filter UNLESS they write into their own
    # isolated worktree (blast radius = the worktree, reviewable/mergeable).
    read_only = isolation != "worktree"
    task_id = start_detached_agent(
        full_task,
        agent_type=agent_type,
        session_id=session_id,
        model_id=model_id,
        effort_label=effort_label,
        read_only=read_only,
        isolation=isolation,
    )
    return json.dumps(
        {
            "task_id": task_id,
            "status": "started",
            "detached": True,
            "hint": (
                (
                    "Running in the background (isolated worktree; on completion "
                    "its changes are applied uncommitted to the originating "
                    "branch and the worktree is removed). "
                    if isolation == "worktree"
                    else "Running in the background (read-only). "
                )
                + "Poll with task_status/"
                "task_output; a completion update will be injected into a later "
                "turn. Do not wait idle — continue other work."
            ),
        }
    )


def _record_agent_cost(session_id: str, model_id: str | None, result) -> None:
    """Roll a finished agent's spend into session_costs (best-effort).

    Rows are keyed "<model_key>_agent" so pricing resolves by longest-prefix
    and the breakdown shows agent spend separately from interactive turns.
    """
    usage = getattr(result, "usage", None) or {}
    if not (usage.get("input_tokens") or usage.get("output_tokens")):
        return
    try:
        from server.agents.providers import model_key_for_id
        from server.costs.tracker import estimate_cost, record_turn

        model_key = model_key_for_id(model_id or "") or "unknown"
        record_turn(
            session_id=session_id,
            turn_number=0,
            model=f"{model_key}_agent",
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=estimate_cost(
                model_key,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_read_tokens", 0),
                usage.get("cache_creation_tokens", 0),
            ),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_tokens", 0),
        )
    except Exception as e:
        log.debug("agent cost rollup failed: %s", e)


async def execute_spawn_agent(
    tool_input: dict,
    session_id: str,
    model_id: str | None = None,
    effort_label: str | None = None,
    *,
    parent_agent_id: str | None = None,
    depth: int = 0,
    team_id: str | None = None,
    event_channel: str | None = None,
) -> str:
    """Spawn a single agent with full tool loop. The agent inherits the
    session-selected model (model_id) AND the session's clamped effort,
    unless the call overrides the model: the top-level ``model`` param (a
    chat_models config key) wins, else ``agent_definition.model``. An
    override that cannot be honoured (unknown key, on-device model) falls
    back to the session model and the warning rides the tool result — the
    same validated-never-silent contract as a workflow agent() override
    (server/agents/model_resolve.py).

    ``detach: true`` runs it in the background instead: the tool returns a
    task_id immediately, progress and completion ride the unified task
    registry (task_status/task_output + a task card + next-turn injection).

    ``agent_definition`` (instead of a registered ``agent_type`` name) builds
    an EPHEMERAL AgentConfig at call time (server/agents/custom_config.py),
    never written to disk. It is registered under a synthesized resolution
    key and that key is used as ``agent_type`` for the rest of this function —
    the same team scaffolding, run_agent call, and (for detach: true) the same
    unmodified detached-agent path (server/tasks/agents.py) that a named type
    goes through, so the ephemeral config gets the identical WRITE_TOOLS /
    read-only-unless-worktree-isolated guardrails with no separate, laxer
    code path. ``result.agent_type`` and any team/event display always show
    the ephemeral definition's clean name, never the synthesized key.

    ``parent_agent_id``/``depth``/``team_id``/``event_channel`` are set by
    server.tool_router.route_tool when this call originates from INSIDE
    another agent's own turn (server.agents.runtime.agent_nesting_ctx) — a
    nested spawn must inherit the parent's recursion depth (the MAX_DEPTH
    guard in run_agent() otherwise never trips, since every call through this
    function defaults to depth=0) and its team_id/event_channel (so the
    child's live progress nests under the SAME team card as its parent,
    instead of a fresh, disconnected one). A top-level call from interactive
    chat leaves all four at their defaults, exactly as before this parameter
    set existed.
    """
    from server.agents.model_resolve import resolve_model_override
    from server.agents.runtime import run_agent

    task = tool_input.get("task", "")
    context = tool_input.get("context", "")
    isolation = tool_input.get("isolation") or "none"

    agent_definition = tool_input.get("agent_definition")
    if not isinstance(agent_definition, dict):
        agent_definition = None

    requested_model = tool_input.get("model") or ((agent_definition or {}).get("model") or None)
    model_id, model_key, model_warning = resolve_model_override(requested_model, model_id)
    if model_warning:
        log.warning("spawn_agent model override: %s", model_warning)

    if parent_agent_id is not None:
        # Nested: this spawn_agent call was made FROM INSIDE another agent's
        # own turn (server.tool_router.route_tool read the ambient
        # server.agents.runtime.agent_nesting_ctx). No fresh team wrapper —
        # the child's live progress nests directly under the PARENT's own
        # team card (team_id/event_channel inherited from it), and depth is
        # the parent's own depth + 1 so run_agent's MAX_DEPTH recursion guard
        # actually holds (every OTHER call through this function defaults to
        # depth=0, which would let an agent-spawns-agent chain recurse
        # forever). Mirrors the pre-migration inline _handle_spawn_agent
        # exactly: no detach, no ephemeral agent_definition, no isolation — a
        # nested spawn never supported those, and this preserves that scope
        # rather than quietly widening it.
        result = await run_agent(
            task,
            agent_type=tool_input.get("agent_type", "general"),
            effort_label=effort_label,
            parent_agent_id=parent_agent_id,
            session_id=session_id,
            context=context,
            depth=depth + 1,
            # Without an explicit override the child inherits the parent's
            # (session) model rather than its agent-type default, so the
            # whole tree uses one model.
            model_id_override=model_id,
            team_id=team_id,
            event_channel=event_channel,
        )
        warning_line = f"[model override] {model_warning}\n" if model_warning else ""
        return (
            f"{warning_line}"
            f"[Agent {result.agent_id} ({result.agent_type})] "
            f"Status: {result.status}, Turns: {result.turns_used}\n"
            f"Output:\n{result.output}"
        )

    # `agent_type` is the string threaded into run_agent/get_agent_config for
    # LOOKUP; `display_agent_type` is what shows up in events/labels. They
    # diverge only for an ephemeral definition (lookup = synthesized key,
    # display = the clean name the model gave it).
    agent_type = tool_input.get("agent_type", "general")
    display_agent_type = agent_type
    ephemeral_meta: dict | None = None

    if agent_definition is not None and str(agent_definition.get("name") or "").strip():
        from server.agents.custom_config import register_ephemeral_type

        agent_type, _ephemeral_config, ephemeral_meta = register_ephemeral_type(
            session_id, agent_definition
        )
        display_agent_type = ephemeral_meta["name"]

    if tool_input.get("detach"):
        out = _start_detached_from_tool(
            task, agent_type, context, session_id, model_id, effort_label, isolation
        )
        if ephemeral_meta or requested_model:
            payload = json.loads(out)
            if ephemeral_meta:
                payload["ephemeral_type"] = ephemeral_meta
            if requested_model:
                # Empty model_key ⇒ the override fell back to the session model.
                payload["model_key"] = model_key or None
                if model_warning:
                    payload["model_warning"] = model_warning
            out = json.dumps(payload)
        return out

    # Wrap the single agent in a one-member "team" so its live tool_call/
    # tool_result/text events render in the rich TeamReportCard (the same view
    # team_create gets) instead of the detail-less AgentCard summary. Without a
    # team_id the frontend fold drops every per-agent event, so a bare
    # spawn_agent was only ever clickable-when-done and showed no tool log.
    # Registering in _teams (with the run wrapped in a named task) is what makes
    # the card's Stop button real: POST /api/teams/{team_id}/stop cancels the
    # task exactly like a team_create fan-out.
    import asyncio

    from server.agent_tools.teams import _teams
    from server.agents.event_bus import event_bus

    team_id = uuid.uuid4().hex[:8]
    agent_label = _spawn_label(task)
    _teams[team_id] = {
        "name": agent_label,
        "description": "",
        "session_id": session_id,
        "agents": [],
    }
    if session_id:
        event_bus.publish(
            session_id,
            {
                "phase": "team_started",
                "team_id": team_id,
                "team_name": agent_label,
                "description": "",
                "agents": [
                    {
                        "name": agent_label,
                        "task": task,
                        "agent_type": display_agent_type,
                        "role": "team",
                    }
                ],
            },
        )

    run_task = asyncio.ensure_future(
        run_agent(
            task,
            agent_type=agent_type,
            session_id=session_id,
            context=context,
            agent_name=agent_label,
            team_id=team_id,
            model_id_override=model_id,
            effort_label=effort_label,
            isolation=isolation,
        )
    )
    _teams[team_id]["task"] = run_task

    stopped_by_user = False
    try:
        result = await run_task
    except asyncio.CancelledError:
        if not _teams.get(team_id, {}).get("stop_requested"):
            # Outer turn cancelled (ESC / stream disconnect) — propagate.
            run_task.cancel()
            raise
        # Stop button: the agent already published its "stopped" event; give the
        # model an honest result instead of an exception.
        stopped_by_user = True
        result = None
    finally:
        _teams.get(team_id, {}).pop("task", None)
        # team_completed must fire on EVERY exit — skipping it on the exception
        # paths left the card stuck at "running" forever.
        if session_id:
            event_bus.publish(
                session_id,
                {
                    "phase": "team_completed",
                    "team_id": team_id,
                    "team_name": agent_label,
                    "agents_completed": 1,
                },
            )

    if stopped_by_user:
        stopped_payload = {
            "agent_id": "",
            "agent_type": display_agent_type,
            "team_id": team_id,
            "status": "stopped",
            "output": "[Stopped by user]",
        }
        if ephemeral_meta:
            stopped_payload["ephemeral_type"] = ephemeral_meta
        return json.dumps(stopped_payload)

    _record_agent_cost(session_id, model_id, result)

    # Pre-flight failures (data-retention gate, no cloud model, depth cap)
    # return status="failed" WITHOUT emitting any per-agent event — publish a
    # synthetic one so the card's row shows the failure instead of a
    # forever-pending spinner under a "completed" team.
    if session_id and result.status == "failed" and not result.agent_id:
        event_bus.publish(
            session_id,
            {
                "phase": "failed",
                "team_id": team_id,
                "agent_name": agent_label,
                "agent_type": display_agent_type,
                "error": (result.output or "")[:500],
            },
        )

    result_payload = {
        "agent_id": result.agent_id,
        "agent_type": result.agent_type,
        "team_id": team_id,
        "status": result.status,
        "turns_used": result.turns_used,
        "tools_called": result.tools_called,
        "usage": result.usage,
        "output": result.output,
    }
    if requested_model:
        # The key the work ACTUALLY ran on (empty ⇒ fell back to the session
        # model), plus the reason when the override could not be honoured.
        result_payload["model_key"] = model_key or None
        if model_warning:
            result_payload["model_warning"] = model_warning
    if ephemeral_meta:
        result_payload["ephemeral_type"] = ephemeral_meta
    return json.dumps(result_payload)


def execute_send_message(tool_input: dict, from_id: str = "main") -> str:
    """Send inter-agent message.

    ``from_id`` defaults to "main" (interactive chat, i.e. no particular
    agent is the sender). server.tool_router.route_tool passes the REAL
    calling agent's id here when this call originates from inside an
    agent's own turn (server.agents.runtime.agent_nesting_ctx), so a
    receiving agent's mailbox shows the true sender instead of "main".
    """
    from server.agents.messaging import message_bus

    to_id = tool_input.get("to_agent_id", "")
    content = tool_input.get("content", "")
    is_broadcast = tool_input.get("broadcast", False)

    if is_broadcast:
        count = message_bus.broadcast(from_id, content)
        return json.dumps({"sent": True, "broadcast": True, "recipients": count})
    elif to_id:
        message_bus.send(from_id, to_id, content)
        return json.dumps({"sent": True, "to": to_id})
    else:
        return json.dumps({"error": "specify to_agent_id or set broadcast=true"})


def execute_list_agents(session_id: str) -> str:
    """List active agents."""
    from server.agents.registry import agent_registry

    agents = agent_registry.list_all(session_id)
    return json.dumps(
        {
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "agent_type": a.agent_type,
                    "status": a.status,
                    "task": a.task[:200],
                    "parent_id": a.parent_id,
                    "model": a.model,
                }
                for a in agents
            ],
            "count": len(agents),
        }
    )
