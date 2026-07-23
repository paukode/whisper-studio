"""FastAPI router for the declarative approval system.

One endpoint:
- POST /api/approval/execute — single hub the frontend calls when the
  user clicks Yes on an approval card. Looks up the spec by action,
  runs its executor, returns {ok, output?, error?}.

The frontend does not fetch the category list from the backend. It
seeds its session-memory approval state from a small hardcoded default
(write/delete/cli) and treats any unknown category as "ask" lazily, so
no dynamic categories fetch exists.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import registry

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/approval", tags=["approval"])


class ExecuteRequest(BaseModel):
    action: str
    payload: dict = {}


@router.post("/execute")
async def approval_execute(req: ExecuteRequest):
    spec = registry.get(req.action)
    if not spec:
        log.warning("approval/execute: unknown action %r", req.action)
        raise HTTPException(status_code=404, detail=f"Unknown approval action: {req.action}")

    # Snapshot the connected workspace so we can tell the frontend to switch
    # panels if this action changed it (e.g. git_clone with open=true). We
    # detect the switch generically by diffing the path instead of teaching
    # this hub about specific actions — connect_workspace is the only thing
    # that mutates it, so a changed value is an unambiguous "open" signal.
    from server.workspace import get_workspace_path

    ws_before = get_workspace_path()
    try:
        outcome = await spec.executor(req.payload or {})
    except Exception as e:  # noqa: BLE001 - executor is user-registered
        log.exception("approval/execute: %s failed", req.action)
        return {"ok": False, "error": f"Executor crashed: {e}"}

    # Invalidate /api/git/changes cache for write-class categories so the
    # panel sees the new file state on the next refresh.
    if outcome.ok and spec.category in ("write", "delete", "cli"):
        try:
            from server.git.router import invalidate_changes_cache

            invalidate_changes_cache()
        except Exception:
            pass

    ws_after = get_workspace_path()
    ws_folder_opened = ws_after if (outcome.ok and ws_after and ws_after != ws_before) else None

    return {
        "ok": outcome.ok,
        "output": outcome.output,
        "error": outcome.error,
        "ws_folder_opened": ws_folder_opened,
    }
