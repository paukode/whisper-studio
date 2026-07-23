"""HTTP API for CI watch + autofix.

Thin wrappers over the ci package: a one-shot status snapshot, a detached watch
launcher (progress/result land on the session event stream), and an autofix
planner that returns the approvable workflow script. Everything is read-only
except ``watch``, which only reads GitHub; the autofix write path is gated
downstream by the workflow approval card.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.ci import autofix, manager, provider

router = APIRouter(prefix="/api/ci", tags=["ci"])


def _resolve(branch: str, cwd: str) -> tuple[str, str]:
    if not cwd:
        try:
            from server.workspace.state import get_workspace_path

            cwd = get_workspace_path() or os.getcwd()
        except Exception:
            cwd = os.getcwd()
    if not branch:
        try:
            from server.git.core import get_branch

            branch = get_branch(cwd) or "HEAD"
        except Exception:
            branch = "HEAD"
    return branch, cwd


@router.get("/status")
async def ci_status(branch: str = "", cwd: str = ""):
    b, c = _resolve(branch, cwd)
    if not provider.gh_available():
        return {"available": False, "branch": b}
    return await asyncio.to_thread(manager.status_snapshot, b, c)


@router.post("/watch")
async def ci_watch(request: Request):
    body = await request.json()
    if not provider.gh_available():
        return JSONResponse({"error": "gh not available"}, status_code=400)
    b, c = _resolve((body.get("branch") or "").strip(), (body.get("cwd") or "").strip())
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
    b, c = _resolve((body.get("branch") or "").strip(), (body.get("cwd") or "").strip())
    run = await asyncio.to_thread(provider.latest_run, b, c)
    if not run:
        return JSONResponse({"error": f"no runs for branch {b}"}, status_code=404)
    if not provider.is_failing(run):
        return {
            "branch": b,
            "failing": False,
            "summary": "latest run isn't failing",
            "script": None,
        }
    full = await asyncio.to_thread(provider.get_run, run["run_id"], c)
    plan = await asyncio.to_thread(
        autofix.plan_autofix, full or run, c, session_id=body.get("session_id") or ""
    )
    return plan
