"""Content search across the workspace: per-file git history and ripgrep.

Every subprocess runs on a worker thread (asyncio.to_thread). Run inline in
these async handlers, a slow grep or git log stalled the whole event loop, so
other sessions' streams and a Disconnect click waited behind it.
"""

import asyncio
import json
import os
import subprocess

from fastapi import Request
from fastapi.responses import Response

from .. import router
from ..paths import _ws_validate_path
from ..state import get_workspace_path


@router.get("/file-history")
async def ws_file_history(request: Request, limit: int = 30):
    """Git log for a specific file. A git failure (not a repository, a bad
    path) is an error with git's message, never an empty history."""
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    path = request.query_params.get("path", "")
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws) or not os.path.isfile(full):
        return Response(
            content=json.dumps({"error": "File not found"}),
            status_code=404,
            media_type="application/json",
        )
    result = await asyncio.to_thread(
        subprocess.run,
        ["git", "log", f"--max-count={limit}", "--format=%H|%an|%ar|%s", "--", path],
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        first = (result.stderr.strip().splitlines() or ["git log failed"])[0]
        return Response(
            content=json.dumps({"error": first}), status_code=500, media_type="application/json"
        )
    history = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) == 4:
            history.append(
                {"hash": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]}
            )
    return {"path": path, "history": history}


@router.get("/grep")
async def ws_grep_endpoint(request: Request, limit: int = 50):
    """Content search within a scoped directory."""
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    pattern = request.query_params.get("pattern", "")
    scope = request.query_params.get("scope", ".")
    if not pattern:
        return Response(
            content=json.dumps({"error": "pattern required"}),
            status_code=400,
            media_type="application/json",
        )
    search_dir = os.path.join(ws, scope)
    if not _ws_validate_path(search_dir, ws):
        return Response(
            content=json.dumps({"error": "Invalid scope"}),
            status_code=403,
            media_type="application/json",
        )
    # Shared resolver (WHISPER_RG_PATH -> PATH -> pip ripgrep wheel); a bare
    # "rg" would miss all of those under a packaged app's minimal PATH.
    # Imported lazily: the server.search package pulls server.workspace back
    # in at import time, so a top-level import here is circular. A missing rg
    # raises with the resolver's reason (a 500), never a different search.
    from server.search.engine import get_rg_path

    rg_bin = get_rg_path()
    # "--" ends the options: a pattern such as "--pre=<program>" is searched
    # for, never read as an rg flag.
    result = await asyncio.to_thread(
        subprocess.run,
        [rg_bin, "--json", "-m", str(limit), "-i", "--", pattern, search_dir],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=ws,
    )
    results = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get("type") == "match":
                match_data = data["data"]
                file_path = match_data["path"]["text"]
                rel = os.path.relpath(file_path, ws) if os.path.isabs(file_path) else file_path
                line_num = match_data["line_number"]
                text = match_data["lines"]["text"].strip()
                results.append({"path": rel, "line": line_num, "content": text[:200]})
        except (json.JSONDecodeError, KeyError):
            continue
        if len(results) >= limit:
            break
    # rg exits 2 on an error; matches found despite one unreadable file stay.
    if result.returncode == 2 and not results:
        return Response(
            content=json.dumps({"error": result.stderr.strip() or "rg failed"}),
            status_code=500,
            media_type="application/json",
        )
    return {"pattern": pattern, "scope": scope, "results": results}
