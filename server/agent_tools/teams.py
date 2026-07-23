"""Multi-agent teams: create a named team of parallel agents (cancellable via
POST /api/teams/{team_id}/stop) and disband it. The in-memory ``_teams`` store
is shared with server/chat/routes.py, which reads it to stop a running team.
"""

import asyncio
import json
import uuid

from .spawn import _record_agent_cost

# In-memory team store
_teams: dict[str, dict] = {}


def _terminal_paragraph(text: str, max_chars: int = 300) -> str:
    """Return the final non-empty paragraph of ``text``, capped at ``max_chars``.

    Agents conclude with their findings — head-slicing keeps the opening
    narration and loses the conclusion. Walk from the end backwards so the
    visible summary is the actual finding.
    """
    if not text:
        return ""
    cleaned = text.strip()
    if not cleaned:
        return ""
    paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    tail = paragraphs[-1] if paragraphs else cleaned
    if len(tail) <= max_chars:
        return tail
    return "…" + tail[-(max_chars - 1) :]


async def execute_team_create(
    tool_input: dict,
    model_id: str,
    session_id: str = "default",
    effort_label: str | None = None,
) -> tuple[str, dict]:
    """Spawn multiple agents in parallel with full tool loops."""
    from server.agents.event_bus import event_bus
    from server.agents.runtime import run_agent as run_agent_fn

    team_name = tool_input.get("team_name", "team")
    description = tool_input.get("description", "")
    agents = tool_input.get("agents", [])
    session_id = tool_input.get("session_id", session_id)

    team_id = uuid.uuid4().hex[:8]
    _teams[team_id] = {
        "name": team_name,
        "description": description,
        "session_id": session_id,
        "agents": [],
    }

    # Announce the team up front so the UI can render the card scaffold and
    # group subsequent per-agent events.
    if session_id:
        event_bus.publish(
            session_id,
            {
                "phase": "team_started",
                "team_id": team_id,
                "team_name": team_name,
                "description": description,
                "agents": [
                    {
                        "name": a.get("name"),
                        "task": a.get("task", ""),
                        "agent_type": a.get("agent_type", "general"),
                        "role": "team",
                    }
                    for a in agents
                ],
            },
        )

    async def _run_one(agent_spec):
        task = agent_spec.get("task", "")
        agent_type = agent_spec.get("agent_type", "general")
        name = agent_spec.get("name")
        ctx = f"Team: {team_name}. {description}"
        try:
            result = await run_agent_fn(
                task,
                agent_type=agent_type,
                session_id=session_id,
                context=ctx,
                agent_name=name,
                team_id=team_id,
                # Every team member uses the session-selected model + effort.
                model_id_override=model_id,
                effort_label=effort_label,
            )
            _record_agent_cost(session_id, model_id, result)
            return {
                "name": name,
                "agent_id": result.agent_id,
                "agent_type": result.agent_type,
                "usage": result.usage,
                "task": task,
                "result": result.output[-2000:],
                "status": result.status,
                "turns_used": result.turns_used,
            }
        except Exception as e:
            return {
                "name": name,
                "task": task,
                "result": str(e),
                "status": "error",
            }

    # Run the members through a named task so the stop endpoint
    # (POST /api/teams/{team_id}/stop) can cancel the whole team mid-flight.
    gather_task = asyncio.ensure_future(asyncio.gather(*[_run_one(a) for a in agents]))
    _teams[team_id]["task"] = gather_task
    stopped_by_user = False
    try:
        agent_results = await gather_task
    except asyncio.CancelledError:
        # The stop endpoint sets stop_requested BEFORE cancelling, so the
        # intent is explicit — inferring it from gather_task.cancelled() is
        # unreliable (the gather can surface a child's CancelledError as a
        # plain exception before its own state flips to cancelled).
        if not _teams.get(team_id, {}).get("stop_requested"):
            # The OUTER turn was cancelled (ESC / stream disconnect) — stop
            # the members and let the cancellation propagate normally.
            gather_task.cancel()
            raise
        # Stop-team path: the members already published their "stopped"
        # events; report what we know and give the model an honest result.
        stopped_by_user = True
        agent_results = [
            {
                "name": a.get("name"),
                "task": a.get("task", ""),
                "agent_type": a.get("agent_type", "general"),
                "result": "[Stopped by user]",
                "status": "stopped",
            }
            for a in agents
        ]
    finally:
        _teams.get(team_id, {}).pop("task", None)

    _teams[team_id]["agents"] = list(agent_results)

    summary = "\n".join(
        f"- {r['name']} ({r.get('agent_type', 'general')}): {_terminal_paragraph(r['result'])}"
        for r in agent_results
    )
    if stopped_by_user:
        summary = "Team stopped by user before completion.\n" + summary

    if session_id:
        event_bus.publish(
            session_id,
            {
                "phase": "team_completed",
                "team_id": team_id,
                "team_name": team_name,
                "agents_completed": len(agent_results),
            },
        )

    return json.dumps(
        {
            "team_id": team_id,
            "team_name": team_name,
            "agents_completed": len(agent_results),
            "summary": summary,
        }
    ), {
        "team_id": team_id,
        "team_name": team_name,
        "description": description,
        "agents": list(agent_results),
    }


def execute_team_delete(tool_input: dict) -> str:
    team_id = tool_input.get("team_id", "")
    if team_id in _teams:
        team = _teams.pop(team_id)
        return json.dumps({"deleted": True, "team_name": team.get("name")})
    return json.dumps({"error": f"Team {team_id} not found"})
