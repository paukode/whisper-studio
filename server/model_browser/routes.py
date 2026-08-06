"""REST surface for the in-app model browser (``/api/models/browse/*``).

Mounted under the same ``/api/models`` prefix as the download manager but on a
distinct ``/browse`` sub-path, so the static browse routes never collide with
the manager's ``/{key}`` routes. Search/detail are read-only; install/uninstall
mutate the user config and the download queue.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from server.model_browser import service

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/models/browse", tags=["model-browser"])


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


@router.get("/search")
async def browse_search(
    q: str | None = None,
    author: str | None = None,
    sort: str = "trending",
    all: str | None = None,
):
    """Search GGUF chat models. Trusted authors by default; ``all=1`` widens to
    all of HF. Only engine-runnable architectures are returned."""
    import asyncio

    results = await asyncio.to_thread(
        service.search,
        q,
        author,
        sort,
        _truthy(all),
    )
    return {"results": results, "count": len(results)}


@router.get("/repo/{repo_id:path}")
async def browse_repo(repo_id: str):
    """The quant picker for one repo (shards folded, mmproj hidden)."""
    import asyncio

    detail = await asyncio.to_thread(service.repo_detail, repo_id)
    return detail


@router.post("/install")
async def browse_install(request: Request):
    """Install a chosen quant: write the config entry and enqueue the download."""
    import asyncio

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")
    repo_id = body.get("repo_id")
    filename = body.get("filename")
    label = body.get("label")
    n_ctx = body.get("n_ctx")
    if not isinstance(repo_id, str) or not isinstance(filename, str):
        raise HTTPException(status_code=400, detail="repo_id and filename are required strings.")
    if n_ctx is not None:
        try:
            n_ctx = int(n_ctx)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="n_ctx must be an integer.") from e
    try:
        result = await asyncio.to_thread(service.install_model, repo_id, filename, label, n_ctx)
    except service.InstallError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@router.delete("/{key}/entry")
async def browse_remove_entry(key: str):
    """Remove a model from the list + picker entirely.

    Deletes a user-added entry outright (weights + config), or tombstones a
    shipped default into ``chat_models_disabled`` (weights deleted if present).
    The distinct ``/entry`` suffix keeps it clear of the ``/{key}`` uninstall."""
    import asyncio

    result = await asyncio.to_thread(service.remove_from_list, key)
    return {**result, "message": "Model removed from the list."}


@router.delete("/{key}")
async def browse_uninstall(key: str):
    """Uninstall a browser-installed model: stop it, delete files, drop config."""
    import asyncio

    try:
        result = await asyncio.to_thread(service.uninstall_model, key)
    except service.InstallError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {**result, "message": "Model uninstalled."}
