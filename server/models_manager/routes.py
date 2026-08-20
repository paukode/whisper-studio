"""REST surface for the in-app model download manager (``/api/models/*``).

Static paths (/catalog, /status) are declared before the ``/{key}`` routes so
they can never be captured as a key. The bare ``GET /api/models`` chat-model
listing lives in server/chat/routes.py and is untouched by this prefix.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server.models_manager import manager, sizes
from server.models_manager.catalog import ModelEntry, bytes_on_disk, entries, get_entry

router = APIRouter(prefix="/api/models", tags=["models"])


def _entry_or_404(key: str) -> ModelEntry:
    entry = get_entry(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown model: {key}")
    return entry


@router.get("/catalog")
async def get_catalog():
    manager.reap()
    all_entries = entries()
    sizes.prefetch(all_entries)  # background; fallbacks answer until it lands
    models = []
    for entry in all_entries:
        state, error = manager.state_of(entry)
        models.append(
            {
                "key": entry.key,
                "label": entry.label,
                "group": entry.group,
                "repo_id": entry.repo_id,
                "note": entry.note,
                "installed": state == "installed",
                "size_bytes_estimate": sizes.estimate(entry),
                "bytes_on_disk": bytes_on_disk(entry),
                # The one shared progress fraction — every surface renders
                # this, never its own bytes/size arithmetic.
                "progress": manager.progress_of(entry, state),
                "state": state,
                # 1-based position while state == "queued", else null.
                "queue_position": manager.queue_position(entry.key),
                "error": error,
            }
        )
    return {"models": models}


@router.get("/status")
async def get_status():
    """Cheap poll target while a download runs: key -> live state."""
    manager.reap()
    out = {}
    for entry in entries():
        state, error = manager.state_of(entry)
        out[entry.key] = {
            "state": state,
            "bytes_on_disk": bytes_on_disk(entry),
            "size_bytes_estimate": sizes.estimate(entry),
            "progress": manager.progress_of(entry, state),
            "queue_position": manager.queue_position(entry.key),
            "error": error,
        }
    return {"models": out}


@router.post("/{key}/download")
async def start_download(key: str):
    """Start a download — or queue it when both slots are busy (HTTP 200
    either way; only "already installed" is a 409). A duplicate request for a
    running/queued download is a benign no-op reporting the current state."""
    entry = _entry_or_404(key)
    try:
        result = manager.start_download(entry)
    except manager.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    queued = result in ("queued", "already_queued")
    return {
        "started": result == "started",
        "queued": queued,
        "key": key,
        "state": "queued" if queued else "downloading",
        "queue_position": manager.queue_position(key),
    }


@router.post("/{key}/cancel")
async def cancel_download(key: str):
    """Cancel a running download, or dequeue a queued one."""
    entry = _entry_or_404(key)
    try:
        result = manager.cancel(entry)
    except manager.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"cancelled": True, "dequeued": result == "dequeued", "key": key}


@router.delete("/{key}")
async def delete_model(key: str):
    entry = _entry_or_404(key)
    try:
        result = manager.delete(entry)
    except manager.Conflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    message = "Model deleted."
    if result.get("stopped_llama_server"):
        message = "Stopped the local model server that was serving this model, then deleted it."
    elif result.get("dequeued") and not result.get("deleted"):
        message = "Removed from the download queue; nothing was on disk yet."
    return {**result, "key": key, "message": message}
