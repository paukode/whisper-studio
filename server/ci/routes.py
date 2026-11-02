"""HTTP API for CI watch + autofix.

Thin wrappers over the ci package: a one-shot status snapshot, a detached watch
launcher (progress/result land on the session event stream), and an autofix
planner that returns the approvable workflow script. Everything is read-only
except ``watch``, which only reads GitHub; the autofix write path is gated
downstream by the workflow approval card.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.ci import autofix, manager, provider

router = APIRouter(prefix="/api/ci", tags=["ci"])


def _error(e: Exception) -> JSONResponse:
    """A CI target error is the caller's (400); a gh failure is upstream (502)."""
    status = 400 if isinstance(e, manager.CITargetError) else 502
    return JSONResponse({"error": str(e)}, status_code=status)


@router.get("/status")
async def ci_status(branch: str = ""):
    try:
        b, c = manager.resolve_target(branch)
        if not provider.gh_available():
            return {"available": False, "branch": b}
        return await asyncio.to_thread(manager.status_snapshot, b, c)
    except (manager.CITargetError, provider.GhError) as e:
        return _error(e)


@router.post("/watch")
async def ci_watch(request: Request):
    body = await request.json()
    if not provider.gh_available():
        return JSONResponse({"error": "gh not available"}, status_code=400)
    try:
        b, c = manager.resolve_target(body.get("branch") or "")
    except manager.CITargetError as e:
        return _error(e)
    task_id = manager.start_watch(b, c, body.get("session_id") or "")
    return {"task_id": task_id, "branch": b, "status": "watching"}


@router.get("/watch/{task_id}")
async def ci_watch_state(task_id: str):
    """Re-attach a CI card to the EXACT run its watch followed (survives reload),
    instead of falling back to the branch's latest run."""
    state = manager.get_watch(task_id)
    if not state:
        return JSONResponse({"error": "unknown ci watch"}, status_code=404)
    return state


@router.post("/watch/{task_id}/stop")
async def ci_watch_stop(task_id: str):
    return {"stopped": manager.stop_watch(task_id)}


@router.post("/autofix")
async def ci_autofix(request: Request):
    body = await request.json()
    if not provider.gh_available():
        return JSONResponse({"error": "gh not available"}, status_code=400)
    try:
        b, c = manager.resolve_target(body.get("branch") or "")
        run = await asyncio.to_thread(provider.latest_run, b, c)
    except (manager.CITargetError, provider.GhError) as e:
        return _error(e)
    if not run:
        return JSONResponse({"error": f"no runs for branch {b}"}, status_code=404)
    if not provider.is_failing(run):
        return {
            "branch": b,
            "failing": False,
            "summary": "latest run isn't failing",
            "script": None,
        }
    try:
        full = await asyncio.to_thread(provider.get_run, run["run_id"], c)
        return await asyncio.to_thread(
            autofix.plan_autofix, full, c, session_id=body.get("session_id") or ""
        )
    except provider.GhError as e:
        return _error(e)
