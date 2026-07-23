"""Memory REST API — browse, edit, delete, and promote memory files.

Backs the Memory viewer UI. Mounted at /api/memory in main.py.
Same guards as the LLM tools: path-traversal check on every filename,
secret scanning on every write, MEMORY.md is read-only (auto-generated),
and the index is rebuilt after every mutation.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from server.memory.executor import _WRITE_LOCK, _atomic_write
from server.memory.memdir import (
    ENTRYPOINT_NAME,
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    get_global_memory_dir,
    get_memory_dir,
    is_path_within_memory_dir,
    load_memory_index,
    normalize_memory_filename,
    rebuild_index,
)
from server.memory.scan import scan_memory_files
from server.memory.secret_scanner import check_and_block
from server.workspace import get_workspace_path

log = logging.getLogger("whisper-studio")

memory_router = APIRouter(prefix="/api/memory", tags=["memory"])


def _tier_dir(scope: str) -> str:
    """Resolve a scope to its directory or raise a client error."""
    if scope == SCOPE_GLOBAL:
        return get_global_memory_dir()
    if scope == SCOPE_PROJECT:
        ws = get_workspace_path()
        if not ws:
            raise HTTPException(status_code=400, detail="No workspace connected")
        return get_memory_dir(ws)
    raise HTTPException(status_code=400, detail=f"Unknown scope: {scope}")


def _safe_path(memory_dir: str, filename: str) -> str:
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    abs_path = os.path.normpath(os.path.join(memory_dir, filename))
    if not is_path_within_memory_dir(abs_path, memory_dir):
        raise HTTPException(status_code=400, detail="Path escapes memory directory")
    return abs_path


def _guard_index_file(filename: str) -> None:
    if os.path.basename(os.path.normpath(filename)).lower() == ENTRYPOINT_NAME.lower():
        raise HTTPException(
            status_code=400, detail="MEMORY.md is auto-generated and cannot be edited directly"
        )


def _list_tier(scope: str, memory_dir: str) -> dict:
    files = [
        {
            "filename": m.filename,
            "name": m.name,
            "description": m.description,
            "type": m.type,
            "scope": scope,
            "mtime": datetime.fromtimestamp(m.mtime, tz=timezone.utc).isoformat(),
            "size": m.size,
        }
        for m in scan_memory_files(memory_dir)
    ]
    return {"scope": scope, "files": files, "index": load_memory_index(memory_dir)}


@memory_router.get("")
async def list_memory():
    """Both tiers: file manifests + index content. Project tier only when a
    workspace is open."""
    tiers = [_list_tier(SCOPE_GLOBAL, get_global_memory_dir())]
    ws = get_workspace_path()
    if ws:
        tiers.append(_list_tier(SCOPE_PROJECT, get_memory_dir(ws)))
    return {"tiers": tiers, "workspace_connected": bool(ws)}


@memory_router.get("/file")
async def read_memory_file(scope: str, filename: str):
    memory_dir = _tier_dir(scope)
    abs_path = _safe_path(memory_dir, filename)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"Not found: {filename}")
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            return {"scope": scope, "filename": filename, "content": f.read()}
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@memory_router.put("/file")
async def save_memory_file(request: Request):
    """Save raw file content (frontmatter included). Secret-scanned like every
    other memory write; the tier index is rebuilt afterwards."""
    body = await request.json()
    scope = body.get("scope", "")
    filename = body.get("filename", "")
    content = body.get("content", "")

    # Same recallability guard as the LLM tool: force a .md suffix, refuse
    # dotfiles. Runs before the index guard so a bare "memory" normalizes to
    # "MEMORY.md" and is still caught below.
    filename, err = normalize_memory_filename(filename)
    if err:
        raise HTTPException(status_code=400, detail=err)

    _guard_index_file(filename)
    memory_dir = _tier_dir(scope)
    abs_path = _safe_path(memory_dir, filename)

    is_clean, _redacted, findings = check_and_block(content)
    if not is_clean:
        rules = ", ".join(f["label"] for f in findings)
        raise HTTPException(status_code=400, detail=f"Content contains potential secrets ({rules})")

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    try:
        with _WRITE_LOCK:
            _atomic_write(abs_path, content)
            rebuild_index(memory_dir)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    log.info("Memory file saved via API: %s (scope=%s)", filename, scope)
    return {"saved": True, "scope": scope, "filename": filename}


@memory_router.delete("/file")
async def delete_memory_file(scope: str, filename: str):
    _guard_index_file(filename)
    memory_dir = _tier_dir(scope)
    abs_path = _safe_path(memory_dir, filename)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"Not found: {filename}")
    try:
        with _WRITE_LOCK:
            os.remove(abs_path)
            rebuild_index(memory_dir)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    log.info("Memory file deleted via API: %s (scope=%s)", filename, scope)
    return {"deleted": True, "scope": scope, "filename": filename}


@memory_router.post("/promote")
async def promote_memory_file(request: Request):
    """Move a file from the project tier to global (make a fact cross-project).

    Refuses to overwrite an existing global file with the same name; delete
    or rename it first. Both tier indexes are rebuilt.
    """
    body = await request.json()
    filename = body.get("filename", "")

    _guard_index_file(filename)
    project_dir = _tier_dir(SCOPE_PROJECT)
    global_dir = get_global_memory_dir()
    src = _safe_path(project_dir, filename)
    dst = _safe_path(global_dir, filename)

    if not os.path.isfile(src):
        raise HTTPException(status_code=404, detail=f"Not found in project memory: {filename}")
    if os.path.exists(dst):
        raise HTTPException(
            status_code=409,
            detail=f"{filename} already exists in global memory; delete or rename it first",
        )

    try:
        with _WRITE_LOCK:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            rebuild_index(project_dir)
            rebuild_index(global_dir)
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    log.info("Memory file promoted to global: %s", filename)
    return {"promoted": True, "filename": filename}
