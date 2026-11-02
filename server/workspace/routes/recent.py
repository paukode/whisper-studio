"""Recent-workspace list: read, remove one, or clear all unindexed. Indexed
folders are protected from removal so a live index is never orphaned.
"""

import os

from fastapi import Request

from .. import router
from ..state import load_recent_workspaces, save_recent_workspaces


@router.get("/recent")
async def ws_recent():
    return {"recent": load_recent_workspaces()}


def _indexed_recent_paths() -> set[str]:
    """Absolute paths of recents that currently have an index — these are kept
    in the list (removing a recent must never orphan an active index). An
    index store error fails the request, so no recent is dropped on a guess."""
    from server.index import store

    return {os.path.abspath(os.path.expanduser(p)) for p in store.list_indexed_workspaces()}


def _save_recent(recent: list[str]) -> dict:
    save_recent_workspaces(recent)
    return {"recent": recent}


@router.post("/recent/remove")
async def ws_recent_remove(request: Request):
    """Remove ONE folder from the recent list. Indexed folders are protected —
    they stay until their index is removed first."""
    body = await request.json()
    path = body.get("path", "")
    indexed = _indexed_recent_paths()
    recent = load_recent_workspaces()
    recent = [r for r in recent if r != path or os.path.abspath(os.path.expanduser(r)) in indexed]
    return _save_recent(recent)


@router.post("/recent/clear-unindexed")
async def ws_recent_clear_unindexed():
    """Drop every recent that isn't indexed, keeping the indexed ones."""
    indexed = _indexed_recent_paths()
    recent = [
        r for r in load_recent_workspaces() if os.path.abspath(os.path.expanduser(r)) in indexed
    ]
    return _save_recent(recent)
