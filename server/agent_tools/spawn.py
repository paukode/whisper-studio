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
) -> str:
    """Spawn a single agent with full tool loop. The agent inherits the
    session-selected model (model_id) AND the session's clamped effort.

    ``detach: true`` runs it in the background instead: the tool returns a
    task_id immediately, progress and completion ride the unified task
    registry (task_status/task_output + a task card + next-turn injection).
    """
    from server.agents.runtime import run_agent

    task = tool_input.get("task", "")
    agent_type = tool_input.get("agent_type", "general")
    context = tool_input.get("context", "")

    isolation = tool_input.get("isolation") or "none"
    if tool_input.get("detach"):
        return _start_detached_from_tool(
            task, agent_type, context, session_id, model_id, effort_label, isolation
        )

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
                    {"name": agent_label, "task": task, "agent_type": agent_type, "role": "team"}
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
        return json.dumps(
            {
                "agent_id": "",
                "agent_type": agent_type,
                "team_id": team_id,
                "status": "stopped",
                "output": "[Stopped by user]",
            }
        )

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
                "agent_type": agent_type,
                "error": (result.output or "")[:500],
            },
        )

    return json.dumps(
        {
            "agent_id": result.agent_id,
            "agent_type": result.agent_type,
            "team_id": team_id,
            "status": result.status,
            "turns_used": result.turns_used,
            "tools_called": result.tools_called,
            "usage": result.usage,
            "output": result.output,
        }
    )


def execute_send_message(tool_input: dict) -> str:
    """Send inter-agent message."""
    from server.agents.messaging import message_bus

    to_id = tool_input.get("to_agent_id", "")
    content = tool_input.get("content", "")
    is_broadcast = tool_input.get("broadcast", False)

    if is_broadcast:
        count = message_bus.broadcast("main", content)
        return json.dumps({"sent": True, "broadcast": True, "recipients": count})
    elif to_id:
        message_bus.send("main", to_id, content)
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
