"""Navigation and discovery endpoints: directory browsing, listing, filename
search, the native folder picker, and worktree enumeration.
"""

import json
import os
import subprocess

from fastapi import Request
from fastapi.responses import Response

from .. import router
from ..executors import _WORKTREES
from ..filesystem import _ws_list_dir, _ws_search_files
from ..paths import _resolve_path, _ws_validate_path
from ..state import get_workspace_path


@router.get("/browse")
async def ws_browse(path: str = ""):
    target = os.path.expanduser(path) if path else os.path.expanduser("~")
    target = _resolve_path(target)
    if not os.path.isdir(target):
        target = os.path.dirname(target)
        if not os.path.isdir(target):
            target = os.path.expanduser("~")
    target = os.path.realpath(target)
    dirs = []
    entries = []
    # Files in the folder, so the picker can show what's inside the directory
    # you're about to connect (read-only — you connect to the folder, not a
    # single file). Capped so a huge directory can't bloat the response; the
    # full count is returned separately so the UI can say "showing N of M".
    files: list[dict] = []
    FILE_CAP = 250
    try:
        for name in sorted(os.listdir(target)):
            if name.startswith("."):
                continue
            full = os.path.join(target, name)
            # Rich shape with mtime so the UI can offer sort-by-modified.
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                mtime = 0.0
            if os.path.isdir(full):
                dirs.append(name)
                # `dirs` (flat names) is kept verbatim for the legacy template.
                entries.append({"name": name, "mtime": mtime})
            elif os.path.isfile(full):
                files.append({"name": name, "mtime": mtime})
    except PermissionError:
        pass
    # When a folder has more files than the cap, keep the most-recently-modified
    # ones: capping the alphabetical scan order would make the UI's "newest
    # first" sort lie (the genuinely-newest could sit in the unsent tail). The
    # full count is reported separately so the UI can say "showing N of M".
    file_total = len(files)
    if file_total > FILE_CAP:
        files.sort(key=lambda f: f["mtime"], reverse=True)
        files = files[:FILE_CAP]
    return {
        "current": target,
        "parent": os.path.dirname(target),
        "dirs": dirs,
        "entries": entries,
        "files": files,
        "file_total": file_total,
    }


@router.post("/mkdir")
async def ws_mkdir(request: Request):
    body = await request.json()
    path = body.get("path", "").strip()
    if not path:
        return Response(
            content=json.dumps({"error": "Path required"}),
            status_code=400,
            media_type="application/json",
        )
    # Support both absolute paths (for workspace creation) and relative paths (for in-workspace dirs)
    ws = get_workspace_path()
    if ws and not os.path.isabs(path):
        full = os.path.join(ws, path)
        if not _ws_validate_path(full, ws):
            return Response(
                content=json.dumps({"error": "Invalid path"}),
                status_code=403,
                media_type="application/json",
            )
    else:
        full = os.path.expanduser(path)
    if os.path.exists(full):
        return Response(
            content=json.dumps({"error": "Already exists"}),
            status_code=409,
            media_type="application/json",
        )
    try:
        os.makedirs(full)
        return {"path": os.path.realpath(full), "created": True}
    except Exception as e:
        return Response(
            content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json"
        )


@router.get("/list-dir")
async def ws_list_dir_endpoint(path: str = ""):
    """List immediate children of a directory within the workspace (lazy loading)."""
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    if path:
        full = os.path.join(ws, path)
        if not _ws_validate_path(full, ws) or not os.path.isdir(full):
            return Response(
                content=json.dumps({"error": "Directory not found"}),
                status_code=404,
                media_type="application/json",
            )
    entries = _ws_list_dir(ws, path)
    return {"entries": entries}


@router.get("/search-files")
async def ws_search_files_endpoint(q: str = "", limit: int = 100):
    """Search for files matching a query across the entire workspace."""
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    if not q.strip():
        return {"results": []}
    results = _ws_search_files(ws, q.strip(), max_results=min(limit, 500))
    return {"results": results}


@router.get("/pick-folder")
async def ws_pick_folder():
    """Open native OS folder picker dialog and return the selected path."""
    import platform

    system = platform.system()
    if system == "Darwin":
        try:
            result = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'POSIX path of (choose folder with prompt "Select workspace folder")',
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                path = result.stdout.strip().rstrip("/")
                if path:
                    return {"path": path}
            return {"path": None, "cancelled": True}
        except subprocess.TimeoutExpired:
            return {"path": None, "cancelled": True}
    return Response(
        content=json.dumps({"error": "Native folder picker not available on this platform"}),
        status_code=501,
        media_type="application/json",
    )


@router.get("/worktrees")
async def list_worktrees():
    """Feature 8: List all active worktrees."""
    ws = get_workspace_path()
    if not ws:
        return {"worktrees": []}
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=ws,
            timeout=10,
        )
        return {"worktrees": list(_WORKTREES.values()), "git_output": result.stdout.strip()}
    except Exception as e:
        return {"worktrees": list(_WORKTREES.values()), "error": str(e)}
