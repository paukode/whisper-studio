"""Session status API for the Live pane and the Settings "Live preview" panel.

Each row carries ``owner``, the chat session that started the preview. The
Live pane shows and stops only its own chat's preview: its Restart and Stop
send ``session_id``, and a preview another chat owns is refused with 409
naming that chat. The Settings panel is the human kill switch for whatever is
running and sends no session, so it can stop any preview.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.preview.manager import (
    PreviewNotOwned,
    owned_by_other,
    owner_label,
    preview_manager,
    server_exited,
)

router = APIRouter(prefix="/api/preview", tags=["preview"])


def _error(status_code: int, message: str) -> Response:
    return Response(
        content=json.dumps({"error": message}),
        status_code=status_code,
        media_type="application/json",
    )


async def _owned_elsewhere(name: str, owner: str) -> Response:
    return _error(
        409,
        f"The preview '{name}' belongs to {await owner_label(owner)}. Switch to that chat to control it.",
    )


@router.get("/sessions")
async def list_sessions():
    return {"sessions": preview_manager.list_sessions()}


@router.post("/sessions")
async def start_session(request: Request):
    """Start (or restart) a preview session by name for the chat in
    ``session_id``, resolving its command from .whisper/launch.json. Lets the
    Live pane's Restart button bring a stopped dev server back without the
    assistant."""
    from server.preview.manager import start_preview_session

    body = await request.json()
    name = (body.get("name") or "").strip()
    session_id = (body.get("session_id") or "").strip()
    if not name or not session_id:
        return _error(400, "name and session_id required")
    existing = preview_manager.get(name)
    if existing is not None:
        if owned_by_other(existing, session_id):
            return await _owned_elsewhere(name, existing.owner)
        if not server_exited(existing):
            return {"started": True, "name": name, "reused": True}
        # Its dev server exited: the start below replaces it.
    ok, msg = await start_preview_session({"session_name": name, "session_id": session_id})
    if not ok:
        return _error(400, msg)
    return {"started": True, "name": name}


@router.delete("/sessions/{name}")
async def stop_session(name: str, session_id: str = ""):
    """Stop a preview. With ``session_id`` (the Live pane) only that chat's
    own preview is stopped; without it (Settings) any preview is."""
    try:
        stopped = await preview_manager.stop_session(name, caller=session_id.strip())
    except PreviewNotOwned as e:
        return await _owned_elsewhere(name, e.owner)
    if not stopped:
        return _error(404, "No such session")
    return {"stopped": True, "name": name}
