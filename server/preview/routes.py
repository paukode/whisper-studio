"""Session status API for the Settings "Live preview" panel — a user-visible
kill switch for whatever the model has currently spun up."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.preview.manager import preview_manager

router = APIRouter(prefix="/api/preview", tags=["preview"])


@router.get("/sessions")
async def list_sessions():
    return {"sessions": preview_manager.list_sessions()}


@router.post("/sessions")
async def start_session(request: Request):
    """Start (or restart) a preview session by name, resolving its command from
    .whisper/launch.json. Lets the UI's Restart button bring a stopped dev
    server back without the assistant."""
    from server.preview.manager import start_preview_session

    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return Response(
            content=json.dumps({"error": "name required"}),
            status_code=400,
            media_type="application/json",
        )
    if preview_manager.get(name):
        return {"started": True, "name": name, "reused": True}
    ok, msg = await start_preview_session({"session_name": name})
    if not ok:
        return Response(
            content=json.dumps({"error": msg}), status_code=400, media_type="application/json"
        )
    return {"started": True, "name": name}


@router.delete("/sessions/{name}")
async def stop_session(name: str):
    stopped = await preview_manager.stop_session(name)
    if not stopped:
        return Response(
            content=json.dumps({"error": "No such session"}),
            status_code=404,
            media_type="application/json",
        )
    return {"stopped": True, "name": name}
