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


# ── Quality gates: /goal gate add|list|remove|clear ──────────────────────────


@router.get("/{session_id}/goal/gates")
async def list_gates(session_id: str):
    return {"gates": store.get_gates(session_id)}


@router.post("/{session_id}/goal/gates")
async def add_gate(session_id: str, request: Request):
    body = await request.json()
    command = (body.get("command") or "").strip()
    if not command:
        return JSONResponse({"error": "command is required"}, status_code=400)
    return {"ok": True, "gates": store.add_gate(session_id, command)}


@router.delete("/{session_id}/goal/gates/{index}")
async def remove_gate(session_id: str, index: int):
    return {"ok": True, "gates": store.remove_gate(session_id, index)}


@router.delete("/{session_id}/goal/gates")
async def clear_gates(session_id: str):
    store.clear_gates(session_id)
    return {"ok": True, "gates": []}
