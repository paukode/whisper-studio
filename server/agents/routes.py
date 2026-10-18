"""HTTP API for agents: GET /api/agents/{agent_id}/record is the on-disk
record the agent pane shows (meta, progress events, report), and
POST /api/agents/{agent_id}/extend grants a running agent more rounds or
seconds (the pane's Extend action)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.agents import extensions, journal

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("/{agent_id}/record")
async def agent_record(agent_id: str, session_id: str | None = None):
    """The record the pane opens with; live events after it arrive on the
    session event channel as agent_progress frames, merged by ``seq``."""
    rec = await asyncio.to_thread(journal.load_feed, agent_id, session_id)
    if rec is None:
        return JSONResponse({"error": f"no record for agent {agent_id}"}, status_code=404)
    return rec


MAX_EXTRA_ROUNDS = 200
MAX_EXTRA_SECONDS = 4 * 3600


@router.post("/{agent_id}/extend")
async def extend_agent(agent_id: str, request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        rounds = max(0, min(MAX_EXTRA_ROUNDS, int(body.get("rounds") or 0)))
        seconds = max(0.0, min(float(MAX_EXTRA_SECONDS), float(body.get("seconds") or 0.0)))
    except (TypeError, ValueError):
        return JSONResponse({"error": "rounds and seconds must be numbers"}, status_code=400)
    if rounds == 0 and seconds == 0.0:
        return JSONResponse({"error": "nothing to extend: pass rounds or seconds"}, status_code=400)
    ext = extensions.extend(agent_id, rounds=rounds, seconds=seconds)
    if ext is None:
        return JSONResponse(
            {"error": "agent is not running; a finished agent is resumed, not extended"},
            status_code=404,
        )
    return {"ok": True, "agent_id": agent_id, "extension": ext}
