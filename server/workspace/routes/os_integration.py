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


def allowed_roots(*, include_save_locations: bool) -> list[str]:
    """Real paths an absolute file path may live under: the connected
    workspace, every indexed workspace, and (when asked) the folders the
    one-shot save_file flow wrote to, which are never connected or indexed.
    A failing index store or save-locations file raises, so the caller
    answers with the real error instead of a 404."""
    from server.index import store as _index_store  # lazy: workspace<->index cycle

    roots: list[str] = []
    ws = get_workspace_path()
    if ws:
        roots.append(os.path.realpath(ws))
    roots += [os.path.realpath(r) for r in _index_store.list_indexed_workspaces()]
    if include_save_locations:
        roots += [os.path.realpath(r) for r in load_save_locations()]
    return roots


def _inside_any(full: str, roots: list[str]) -> bool:
    return any(full == r or full.startswith(r + os.sep) for r in roots)


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
        inside = _inside_any(full, allowed_roots(include_save_locations=True))
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
        inside = _inside_any(full, allowed_roots(include_save_locations=True))
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
