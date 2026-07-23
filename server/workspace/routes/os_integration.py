"""OS file-manager integration: open a file with its default app, or reveal it
in Finder/Explorer. Reveal accepts absolute paths inside any indexed root so
grounded citations resolve.
"""

import json
import os
import subprocess

from fastapi import Request
from fastapi.responses import Response

from .. import router
from ..paths import _ws_validate_path
from ..state import get_workspace_path


@router.post("/open-with")
async def ws_open_with(request: Request):
    """Open a file with the system default application."""
    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    path = body.get("path", "")
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws) or not os.path.exists(full):
        return Response(
            content=json.dumps({"error": "File not found"}),
            status_code=404,
            media_type="application/json",
        )
    import platform

    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", full])
    elif system == "Linux":
        subprocess.Popen(["xdg-open", full])
    elif system == "Windows":
        os.startfile(full)
    else:
        return Response(
            content=json.dumps({"error": "Unsupported platform"}),
            status_code=500,
            media_type="application/json",
        )
    return {"path": path, "opened": True}


@router.post("/reveal")
async def ws_reveal(request: Request):
    """Reveal a file in the OS file manager (Finder/Explorer) — selects it in
    its folder rather than opening it. Used by the chat 'source' links.

    Accepts a workspace-relative path (resolved against the connected workspace)
    or an absolute path — the latter is allowed only when it lives inside the
    connected workspace or a known indexed workspace, so a grounded citation
    from a *different* indexed folder can still be revealed."""
    body = await request.json()
    path = body.get("path", "")
    if not path:
        return Response(
            content=json.dumps({"error": "File not found"}),
            status_code=404,
            media_type="application/json",
        )
    if os.path.isabs(path):
        full = os.path.realpath(path)
        roots = []
        ws = get_workspace_path()
        if ws:
            roots.append(os.path.realpath(ws))
        try:  # lazy import to avoid a workspace↔index module cycle
            from server.index import store as _index_store

            roots += [os.path.realpath(r) for r in _index_store.list_indexed_workspaces()]
        except Exception:
            pass
        inside = any(full == r or full.startswith(r + os.sep) for r in roots)
        if not inside or not os.path.exists(full):
            return Response(
                content=json.dumps({"error": "File not found"}),
                status_code=404,
                media_type="application/json",
            )
    else:
        ws = get_workspace_path()
        if not ws:
            return Response(
                content=json.dumps({"error": "No workspace"}),
                status_code=400,
                media_type="application/json",
            )
        full = os.path.join(ws, path)
        if not _ws_validate_path(full, ws) or not os.path.exists(full):
            return Response(
                content=json.dumps({"error": "File not found"}),
                status_code=404,
                media_type="application/json",
            )
    import platform

    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", "-R", full])  # reveal + select in Finder
    elif system == "Linux":
        subprocess.Popen(
            ["xdg-open", os.path.dirname(full)]
        )  # no universal "select"; open the folder
    elif system == "Windows":
        subprocess.Popen(["explorer", "/select,", full])
    else:
        return Response(
            content=json.dumps({"error": "Unsupported platform"}),
            status_code=500,
            media_type="application/json",
        )
    return {"path": path, "revealed": True}
