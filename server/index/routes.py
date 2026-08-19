"""HTTP API for the workspace index — status, build, remove, and schedule.

Builds run in a daemon thread (a full index can take seconds to minutes) with
live progress tracked in-process, so the UI can poll ``/status`` and show a
"% done" / "last indexed" badge without blocking the request. Mounted under
``/api/workspace/index``.
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Request

from . import paths, scheduler, store
from .pipeline import build as _build
from .pipeline import is_building, request_cancel

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/workspace/index")

# In-progress builds, keyed by absolute workspace path:
#   {building: bool, done: int, total: int, current: str, error: str|None}
_BUILDS: dict[str, dict] = {}
_lock = threading.Lock()


def _abspath(path: str) -> str:
    import os

    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def _run_build(ws_path: str) -> None:
    key = _abspath(ws_path)

    def _progress(done: int, total: int, current: str) -> None:
        with _lock:
            st = _BUILDS.get(key)
            if st is not None:
                st.update(done=done, total=total, current=current)

    try:
        _build(ws_path, progress=_progress)
        with _lock:
            st = _BUILDS.get(key)  # may be gone if /remove landed mid-build
            if st is not None:
                st.update(building=False, error=None)
    except Exception as e:  # noqa: BLE001 — surface to the UI, don't crash the thread
        log.error("Index build failed for %s: %s", ws_path, e)
        with _lock:
            st = _BUILDS.get(key)
            if st is not None:
                st.update(building=False, error=str(e))


@router.get("/status")
async def index_status(path: str = ""):
    """Index status for a workspace path (defaults to the connected one)."""
    if not path:
        from server.workspace import get_workspace_path

        path = get_workspace_path() or ""
    if not path:
        return {"indexed": False, "building": False}
    key = _abspath(path)
    with _lock:
        build = dict(_BUILDS.get(key, {}))
    out = {
        "path": path,
        "indexed": paths.is_indexed(path),
        "building": bool(build.get("building")),
        "progress": {
            "done": build.get("done", 0),
            "total": build.get("total", 0),
            "current": build.get("current"),
        }
        if build.get("building")
        else None,
        "error": build.get("error"),
    }
    if out["indexed"]:
        out.update(store.stats(path))
    return out


@router.post("/build")
async def index_build(request: Request):
    """Start (or refresh) the index for a workspace in the background."""
    body = await request.json()
    path = (body.get("path") or "").strip()
    if not path:
        return {"error": "path is required"}
    key = _abspath(path)
    with _lock:
        # Already building here, or the scheduled refresh holds the build lock.
        if _BUILDS.get(key, {}).get("building") or is_building(path):
            return {"started": False, "reason": "already building"}
        _BUILDS[key] = {"building": True, "done": 0, "total": 0, "current": None, "error": None}
    threading.Thread(target=_run_build, args=(path,), daemon=True, name="index-build").start()
    return {"started": True}


@router.get("/list")
async def index_list():
    """All indexed workspaces (for the session's search-index picker)."""
    import os

    out = []
    for ws in store.list_indexed_workspaces():
        # list_indexed_workspaces() reports a folder if it has an index under ANY
        # embed backend; only surface (and read) the ones present under the ACTIVE
        # backend, so a read never fabricates an empty active-backend db for a
        # folder indexed under the other embedder.
        if not store.has_index(ws):
            continue
        s = store.stats(ws)
        out.append(
            {
                "path": ws,
                "name": os.path.basename(os.path.normpath(ws)) or ws,
                "files": s.get("files", 0),
                "chunks": s.get("chunks", 0),
                "last_indexed_at": s.get("last_indexed_at"),
            }
        )
    out.sort(key=lambda x: x["name"].lower())
    return {"indexes": out}


def _connected_or(path: str) -> str:
    """The given path, or the connected workspace when blank."""
    if path:
        return path
    from server.workspace import get_workspace_path

    return get_workspace_path() or ""


@router.get("/explore/overview")
async def explore_overview(path: str = ""):
    """The index explorer's landing view: inventory stats, named topic cards
    (one per detected file group), the most salient entities, and the most
    connected files — words and counts, no visual encodings."""
    path = _connected_or(path)
    if not path or not paths.is_indexed(path):
        return {"files": 0, "passages": 0, "entities": 0, "cards": [], "root": ""}
    out = dict(store.explore_overview(path))
    out["root"] = _abspath(path)
    return out


@router.get("/explore/entities")
async def explore_entities(path: str = "", q: str = "", include_low: bool = False):
    """Browse pane + entity search: entities grouped into People, Organizations,
    Topics, and Places with per-name file counts. Names under the salience floor
    are hidden but counted (``hidden``); ``include_low`` shows them."""
    path = _connected_or(path)
    if not path or not paths.is_indexed(path):
        return {"groups": [], "total": 0, "hidden": 0, "root": ""}
    out = dict(store.explore_entities(path, q, include_low))
    out["root"] = _abspath(path)
    return out


@router.get("/explore/entity")
async def explore_entity(path: str = "", name: str = "", label: str = ""):
    """One entity's page: canonical name + label + description, typed facts as
    sentence-ready records with verbatim evidence and line-anchored citations,
    the files that mention it, and its most co-mentioned neighbours."""
    path = _connected_or(path)
    if not path or not paths.is_indexed(path) or not name:
        return {"found": False, "root": ""}
    out = dict(store.explore_entity(path, name, label))
    out["root"] = _abspath(path)
    return out


@router.get("/explore/file")
async def explore_file(path: str = "", file: str = ""):
    """One file's page: the names and topics it mentions, every file sharing
    salient entities with it (computed for this file, never clipped by the
    overview graph's edge cap), and the typed facts stated in it."""
    path = _connected_or(path)
    if not path or not paths.is_indexed(path) or not file:
        return {"found": False, "root": ""}
    out = dict(store.explore_file(path, file))
    out["root"] = _abspath(path)
    return out


@router.post("/cancel")
async def index_cancel(request: Request):
    """Ask an in-progress build to stop after the current file (keeps partial)."""
    body = await request.json()
    path = (body.get("path") or "").strip()
    if not path:
        return {"error": "path is required"}
    return {"cancelling": request_cancel(path)}


@router.post("/remove")
async def index_remove(request: Request):
    body = await request.json()
    path = (body.get("path") or "").strip()
    if not path:
        return {"error": "path is required"}
    store.remove_index(path)
    with _lock:
        _BUILDS.pop(_abspath(path), None)
    # Drop this folder's scheduled job and re-sync the background agent — its
    # settings/DB are gone now, so a lingering job would silently re-create it.
    scheduler.apply_workspace(_abspath(path))
    try:
        from . import agent

        agent.regenerate()
    except Exception:  # noqa: BLE001 — removal must succeed regardless
        pass
    return {"removed": True}


@router.get("/engines")
async def get_index_engines():
    """The engine choices for the LLM index passes (relationship mapping,
    entity descriptions, chunk headers), filtered by the active model mode:
    cloud/hybrid list Haiku plus every on-device model from the config (with
    download state for the picker badges), local lists only downloaded on-device
    models. The mode ships with the rows so the picker can explain an empty list."""
    from server.infrastructure import model_mode as mm

    from . import engines

    mode = mm.current_mode()
    return {"engines": engines.engine_catalog(mode), "mode": mode}


@router.get("/settings")
async def get_index_settings(request: Request):
    """This workspace's own index settings (schedule, typed relations, background
    refresh). Per-folder — falls back to the connected workspace if no path."""
    from . import wssettings

    path = (request.query_params.get("path") or "").strip()
    if not path:
        from server.workspace import get_workspace_path

        path = get_workspace_path() or ""
    return wssettings.get_settings(_abspath(path))


@router.put("/settings")
async def put_index_settings(request: Request):
    """Update one workspace's settings, re-apply its scheduled job, and sync the
    background agent (which wakes at the union of opted-in folders' hours)."""
    from . import agent, wssettings

    body = await request.json()
    path = (body.get("path") or "").strip()
    if not path:
        return {"error": "path is required"}
    ws = _abspath(path)
    patch = {
        k: body[k]
        for k in (
            "schedule",
            "typed_relations",
            "entity_descriptions",
            "chunk_context",
            "ner_model",
            "refresh_when_closed",
        )
        if k in body
    }
    settings = wssettings.update_settings(ws, patch)
    scheduler.apply_workspace(ws)
    agent.regenerate()
    return settings


@router.get("/agent")
async def get_agent():
    """Whether the background refresh helper is installed, and whether this
    platform supports it (macOS). The per-folder on/off lives in /settings."""
    from . import agent

    return agent.status()
