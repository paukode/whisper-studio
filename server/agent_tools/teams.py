"""Multi-agent teams: create a named team of parallel agents (cancellable via
POST /api/teams/{team_id}/stop) and disband it. The in-memory ``_teams`` store
is shared with server/chat/routes.py, which reads it to stop a running team.
"""

import asyncio
import json
import uuid

from server.agents.journal import RELAY_NOTE, REPORT_CHARS, team_manifest
from server.agents.journal import load as load_journal
from server.tasks.events import emit_agent_report

from .spawn import (
    _budget_exceeded_message,
    _concurrency_limit_message,
    _persist_agent_result,
    _record_agent_cost,
    _release_agent_slot,
    _try_reserve_agent_slot,
)

# How long a cancelled team waits for its members' salvage reports to land
# in their journals before the reports are delivered to the session.
SALVAGE_WAIT_S = 5.0

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


def _status_label(r: dict) -> str:
    reason = r.get("stop_reason") or r.get("status") or ""
    turns = r.get("turns_used")
    turns_s = f", {turns} round{'s' if turns != 1 else ''}" if isinstance(turns, int) else ""
    return {
        "completed": "finished" + turns_s,
        "turn_limit": "stopped at the turn limit" + turns_s + ", report written",
        "deadline": "stopped at the time limit" + turns_s + ", report written",
        "cost_cap": "stopped at the cost cap" + turns_s + ", report written",
        "cancelled": "cancelled" + turns_s + ", report assembled by the runtime",
        "error": "failed" + turns_s,
        "stopped": "stopped" + turns_s,
        "failed": "failed" + turns_s,
    }.get(reason, reason + turns_s)


def _team_summary(team_name: str, results: list[dict], stopped_by_user: bool) -> str:
    """What the parent model reads: every member's report in full (bounded),
    never the last paragraph of it, plus the relay obligation."""
    head = f'Team "{team_name}": {len(results)} member report(s). {RELAY_NOTE}'
    if stopped_by_user:
        head = "Team stopped by user before completion. " + head
    blocks = [head]
    for r in results:
        title = f"## {r.get('name') or r.get('agent_id') or 'member'} ({r.get('agent_type', 'general')})"
        title += f" [{_status_label(r)}]"
        if r.get("agent_id"):
            title += f" record: task_output {r['agent_id']}"
        blocks.append(f"{title}\n{str(r.get('result') or '').strip()[:REPORT_CHARS]}")
    return "\n\n".join(blocks)


def _reports_from_journals(
    agents: list[dict], member_ids: list[str], session_id: str
) -> list[dict]:
    """Member rows built from the on-disk records, for members whose run was
    cancelled before it could return a result."""
    rows = []
    for spec, mid in zip(agents, member_ids, strict=True):
        rec = load_journal(mid, session_id or None) or {}
        meta = rec.get("meta") or {}
        report = (rec.get("report") or "").strip() or "[No record was written before the stop]"
        rows.append(
            {
                "name": spec.get("name"),
                "agent_id": mid,
                "agent_type": spec.get("agent_type", "general"),
                "task": spec.get("task", ""),
                "result": report[:REPORT_CHARS],
                "status": "completed" if meta.get("status") == "completed" else "stopped",
                "stop_reason": meta.get("stop_reason") or "cancelled",
                "turns_used": int(meta.get("rounds") or 0),
            }
        )
    return rows


async def _settle_cancelled_members(gather_task: asyncio.Task) -> None:
    """Give cancelled members a bounded moment to write their salvage reports."""
    try:
        await asyncio.wait({gather_task}, timeout=SALVAGE_WAIT_S)
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - best effort, then move on
        pass


async def execute_team_create(
    tool_input: dict,
    model_id: str,
    session_id: str = "default",
    effort_label: str | None = None,
    *,
    parent_agent_id: str | None = None,
    depth: int = 0,
    event_channel: str | None = None,
) -> tuple[str, dict]:
    """Spawn multiple agents in parallel with full tool loops. Progress is
    published on ``event_channel`` (default: the session id).

    ``parent_agent_id``/``depth`` are set by server.tool_router.route_tool
    when this call originates from INSIDE another agent's own turn (the same
    ambient server.agents.runtime.agent_nesting_ctx that spawn_agent already
    reads) — a team spawned from inside a running agent must inherit that
    agent's recursion depth so run_agent's MAX_DEPTH guard actually applies to
    team members. Left at their defaults, every team_create call ran its
    members at depth 0 regardless of how deeply nested the calling agent
    already was, letting a team member's own team_create recurse without
    limit — the mechanism behind a real incident where one request queued 56
    agent slots. A top-level call from interactive chat leaves both at their
    defaults, exactly as before this parameter pair existed.
    """
    progress_channel = event_channel or session_id
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
    if progress_channel:
        event_bus.publish(
            progress_channel,
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

    async def _run_one(agent_spec, member_id: str):
        task = agent_spec.get("task", "")
        agent_type = agent_spec.get("agent_type", "general")
        name = agent_spec.get("name")
        ctx = f"Team: {team_name}. {description}"
        _budget_msg = _budget_exceeded_message(session_id)
        if _budget_msg:
            return {
                "name": name,
                "agent_type": agent_type,
                "task": task,
                "result": _budget_msg,
                "status": "failed",
                "stop_reason": "error",
            }
        if not _try_reserve_agent_slot(session_id):
            return {
                "name": name,
                "agent_type": agent_type,
                "task": task,
                "result": _concurrency_limit_message(),
                "status": "failed",
                "stop_reason": "error",
            }
        try:
            result = await run_agent_fn(
                task,
                agent_type=agent_type,
                session_id=session_id,
                event_channel=event_channel,
                context=ctx,
                agent_name=name,
                team_id=team_id,
                # Every team member uses the session-selected model + effort.
                model_id_override=model_id,
                effort_label=effort_label,
                parent_agent_id=parent_agent_id,
                depth=depth + 1,
                # Pre-assigned so the member's on-disk record can be found
                # even if this call never returns (cancelled team).
                agent_id=member_id,
            )
            _record_agent_cost(session_id, model_id, result)
            _persist_agent_result(session_id, task, agent_type, model_id, result)
            return {
                "name": name,
                "agent_id": result.agent_id or member_id,
                "agent_type": result.agent_type,
                "usage": result.usage,
                "task": task,
                # The report leads (stop note, then the final message); the
                # head is what the parent and the card need, not the tail.
                "result": result.output[:REPORT_CHARS],
                "status": result.status,
                "stop_reason": getattr(result, "stop_reason", result.status),
                "turns_used": result.turns_used,
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {
                "name": name,
                "agent_id": member_id,
                "task": task,
                "result": str(e),
                "status": "error",
                "stop_reason": "error",
            }
        finally:
            _release_agent_slot(session_id)

    # Run the members through a named task so the stop endpoint
    # (POST /api/teams/{team_id}/stop) can cancel the whole team mid-flight.
    member_ids = [uuid.uuid4().hex[:10] for _ in agents]
    _teams[team_id]["member_ids"] = member_ids
    gather_task = asyncio.ensure_future(
        asyncio.gather(*[_run_one(a, mid) for a, mid in zip(agents, member_ids, strict=True)])
    )
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
            # The OUTER turn was cancelled (ESC / stream disconnect / server
            # shutdown) and nobody will read this tool result. Stop the
            # members, let their salvage reports land, and deliver those
            # reports into the session as a row the NEXT turn reads, so the
            # work is not lost with the turn that launched it.
            gather_task.cancel()
            await _settle_cancelled_members(gather_task)
            orphaned = _reports_from_journals(agents, member_ids, session_id)
            _teams[team_id]["agents"] = orphaned
            team_manifest(
                session_id,
                team_id,
                {"team_id": team_id, "team_name": team_name, "members": orphaned},
            )
            emit_agent_report(
                session_id,
                {
                    "team_id": team_id,
                    "team_name": team_name,
                    "description": description,
                    "reason": (
                        "the turn that launched this team ended before it could read "
                        "the results; the members were stopped and their records kept"
                    ),
                    "agents": orphaned,
                },
            )
            raise
        # Stop-team path: the members already published their "stopped"
        # events; give the model each member's salvage report instead of a
        # bare "[Stopped by user]".
        stopped_by_user = True
        await _settle_cancelled_members(gather_task)
        agent_results = _reports_from_journals(agents, member_ids, session_id)
    finally:
        _teams.get(team_id, {}).pop("task", None)

    _teams[team_id]["agents"] = list(agent_results)
    team_manifest(
        session_id,
        team_id,
        {
            "team_id": team_id,
            "team_name": team_name,
            "description": description,
            "members": [
                {k: r.get(k) for k in ("name", "agent_id", "agent_type", "status", "stop_reason")}
                for r in agent_results
            ],
        },
    )

    summary = _team_summary(team_name, agent_results, stopped_by_user)

    if progress_channel:
        event_bus.publish(
            progress_channel,
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
