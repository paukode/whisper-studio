"""Content search across the workspace: per-file git history and ripgrep/grep."""

import json
import os
import subprocess

from fastapi import Request
from fastapi.responses import Response

from .. import router
from ..paths import _ws_validate_path
from ..state import get_workspace_path


@router.get("/file-history")
async def ws_file_history(request: Request):
    """Git log for a specific file."""
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    path = request.query_params.get("path", "")
    limit = int(request.query_params.get("limit", "30"))
    full = os.path.join(ws, path)
    if not _ws_validate_path(full, ws) or not os.path.isfile(full):
        return Response(
            content=json.dumps({"error": "File not found"}),
            status_code=404,
            media_type="application/json",
        )
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}", "--format=%H|%an|%ar|%s", "--", path],
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=10,
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
    except Exception as e:
        return Response(
            content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json"
        )


@router.get("/grep")
async def ws_grep_endpoint(request: Request):
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
    limit = int(request.query_params.get("limit", "50"))
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
    try:
        result = subprocess.run(
            ["rg", "--json", "--max-count", "3", "-m", str(limit), "-i", pattern, search_dir],
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
        return {"pattern": pattern, "scope": scope, "results": results}
    except FileNotFoundError:
        # rg not installed, fall back to grep
        try:
            result = subprocess.run(
                ["grep", "-rn", "-i", "--include=*", pattern, search_dir],
                capture_output=True,
                text=True,
                timeout=15,
            )
            results = []
            for line in result.stdout.strip().split("\n")[:limit]:
                if ":" in line:
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        rel = os.path.relpath(parts[0], ws)
                        results.append(
                            {
                                "path": rel,
                                "line": int(parts[1]) if parts[1].isdigit() else 0,
                                "content": parts[2].strip()[:200],
                            }
                        )
            return {"pattern": pattern, "scope": scope, "results": results}
        except Exception as e:
            return Response(
                content=json.dumps({"error": str(e)}),
                status_code=500,
                media_type="application/json",
            )
    except Exception as e:
        return Response(
            content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json"
        )
