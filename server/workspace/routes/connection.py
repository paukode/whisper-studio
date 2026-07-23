"""Workspace connection lifecycle: /connect, /disconnect, /status."""

import json
import os

from fastapi import Request
from fastapi.responses import Response

from .. import router
from ..filesystem import _ws_list_dir
from ..paths import WORKSPACE_BACKUPS, _resolve_path
from ..state import (
    _check_writable,
    connect_workspace,
    load_workspace_config,
    save_workspace_config,
)


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
    real = connect_workspace(path)
    entries = _ws_list_dir(real)
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
    config = load_workspace_config()
    config["path"] = None
    config["mode"] = "chat"
    save_workspace_config(config)
    WORKSPACE_BACKUPS.clear()
    from server.git.watcher import git_watcher

    git_watcher.set_workspace(None)
    return {"disconnected": True}


@router.get("/status")
async def ws_status():
    config = load_workspace_config()
    ws = config.get("path")
    if not ws or not os.path.isdir(ws):
        return {"connected": False}
    return {"connected": True, "path": ws, "mode": config.get("mode", "chat")}
