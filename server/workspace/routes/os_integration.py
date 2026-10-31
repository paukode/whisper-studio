"""Finder integration: open a file with its default app, or reveal it in
Finder. Reveal accepts absolute paths inside any indexed root so grounded
citations resolve.
"""

import json
import os
import subprocess

from fastapi import Request
from fastapi.responses import Response

from .. import router
from ..paths import _ws_validate_path
from ..state import get_workspace_path, load_save_locations


@router.post("/open-with")
async def ws_open_with(request: Request):
    """Open a file with the system default application.

    Accepts a workspace-relative path (resolved against the connected
    workspace) or an absolute path — the latter is allowed only when it lives
    inside the connected workspace or a known indexed workspace, mirroring
    /reveal below, so a created-file chat link still opens after the user
    switches to a different workspace."""
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
        # Directories the one-shot save_file flow has written to. These
        # files were never 'connected' or 'indexed', so without this they'd
        # 404 here the moment the workspace/index roots above don't cover
        # them — which is the common case, since save_file exists precisely
        # to save somewhere OUTSIDE the connected workspace.
        try:
            roots += [os.path.realpath(r) for r in load_save_locations()]
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
    subprocess.Popen(["open", full])
    return {"path": path, "opened": True}


@router.post("/reveal")
async def ws_reveal(request: Request):
    """Reveal a file in Finder: selects it in its folder rather than opening
    it. Used by the chat 'source' links.

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
        # Directories the one-shot save_file flow has written to. These
        # files were never 'connected' or 'indexed', so without this they'd
        # 404 here the moment the workspace/index roots above don't cover
        # them — which is the common case, since save_file exists precisely
        # to save somewhere OUTSIDE the connected workspace.
        try:
            roots += [os.path.realpath(r) for r in load_save_locations()]
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
    subprocess.Popen(["open", "-R", full])  # reveal + select in Finder
    return {"path": path, "revealed": True}
