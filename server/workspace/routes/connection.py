"""Workspace connection lifecycle: /connect, /disconnect, /status."""

import asyncio
import json
import logging
import os

from fastapi import Request
from fastapi.responses import Response

from .. import router
from ..filesystem import _ws_list_dir
from ..paths import _resolve_path
from ..state import (
    _check_writable,
    connect_workspace,
    disconnect_workspace,
    load_workspace_config,
)

log = logging.getLogger("whisper-studio")


@router.post("/connect")
async def ws_connect(request: Request):
    body = await request.json()
    path = body.get("path", "").strip()
    if not path:
        return Response(
            content=json.dumps({"error": "Path required"}),
            status_code=400,
            media_type="application/json",
        )
    path = os.path.expanduser(path)
    path = _resolve_path(path)
    if not os.path.isdir(path):
        return Response(
            content=json.dumps({"error": "Directory not found"}),
            status_code=404,
            media_type="application/json",
        )
    # by_user: switching away from a workspace here releases it for any turn
    # still running in it, the same as a disconnect.
    real = connect_workspace(path, by_user=True)
    # The config write above stays on the loop so a Disconnect clicked right
    # after is applied in order; the listing is only a read, so it goes to a
    # worker thread instead of holding every other session's stream.
    entries = await asyncio.to_thread(_ws_list_dir, real)
    return {
        "path": real,
        "entries": entries,
        # Advisory hint — see _check_writable docstring. False is a
        # soft warning, not a refusal; the frontend toasts an info note
        # so the user knows writes might fail without blocking them.
        "writable": _check_writable(real),
    }


@router.post("/disconnect")
async def ws_disconnect():
    """Disconnect at once, whatever a session is doing.

    disconnect_workspace is in-memory state plus one small atomic file write,
    so this answers in about a millisecond even mid-turn: it takes no lock a
    turn holds, stops no turn and kills no process a session started. The
    running turn's next workspace tool call is refused instead. A teardown
    step that fails is logged and returned as a warning the UI shows; a failed
    config write returns 500 and leaves the workspace connected.
    """
    try:
        warnings = disconnect_workspace()
    except Exception as e:  # noqa: BLE001 - reported to the UI, not swallowed
        log.exception("workspace disconnect: saving the workspace config failed")
        return Response(
            content=json.dumps(
                {"error": f"Could not save the workspace config, still connected: {e}"}
            ),
            status_code=500,
            media_type="application/json",
        )
    return {"disconnected": True, "warnings": warnings}


@router.get("/status")
async def ws_status():
    config = load_workspace_config()
    ws = config.get("path")
    if not ws or not os.path.isdir(ws):
        return {"connected": False}
    return {"connected": True, "path": ws}
