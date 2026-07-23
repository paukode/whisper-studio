"""HTTP API for session goals: GET/POST/DELETE /api/sessions/{id}/goal."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.goals import store

router = APIRouter(prefix="/api/sessions", tags=["goals"])


@router.get("/{session_id}/goal")
async def get_goal(session_id: str):
    return store.get_goal(session_id)


@router.post("/{session_id}/goal")
async def set_goal(session_id: str, request: Request):
    body = await request.json()
    goal = (body.get("goal") or "").strip()
    if not goal:
        return JSONResponse({"error": "goal is required"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    return {"ok": True, **store.set_goal(session_id, goal, set_at=now)}


@router.delete("/{session_id}/goal")
async def clear_goal(session_id: str):
    store.clear_goal(session_id)
    return {"ok": True}
