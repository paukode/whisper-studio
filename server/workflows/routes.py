"""HTTP + SSE API for workflow runs and saved workflows.

Runs list/detail/journal/stop, a launch endpoint used by the approval card, a
per-run SSE stream (event_bus channel ``workflow:{run_id}``), and saved-workflow
list/approve/delete.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from server.workflows import manager, store

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


def _is_run_terminal(ev: dict) -> bool:
    """True only for the RUN-level completion event.

    manager._finalize publishes the run terminal wrapped as
    ``type="workflow_event"`` with ``phase="completed"``. Each sub-agent ALSO
    emits a raw ``phase:"completed"`` (no ``type``) on the same
    ``workflow:{run_id}`` channel; those must not end the stream, or the first
    agent to finish would cut off the whole run's live events.
    """
    return ev.get("type") == "workflow_event" and ev.get("phase") == "completed"


def _default_model():
    try:
        from server.infrastructure.config import load_config

        cfg = load_config()
        models = cfg.get("chat_models", {}) or {}
        key = cfg.get("default_chat_model")
        model_id = (
            (models.get(key) if key else None)
            or models.get("sonnet")
            or next(iter(models.values()), "")
        )
        return key or "sonnet", model_id
    except Exception:
        return "sonnet", ""


@router.get("/runs")
async def list_runs(session_id: str = ""):
    return {"runs": manager.list_runs(session_id or None)}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    return run


@router.post("/runs")
async def launch_run(request: Request):
    """Launch a run from the approval card (new script) or by saved name."""
    body = await request.json()
    script = (body.get("script") or "").strip()
    name = (body.get("name") or "").strip()
    session_id = body.get("session_id", "")
    args = body.get("args")
    budget_usd = body.get("budget_usd")
    # Honor the session's model (passed through the approval card); fall back to
    # the configured default only when the caller didn't supply one.
    model_id = (body.get("model_id") or "").strip()
    if model_id:
        from server.workflows.tools import _model_key_for

        model_key = _model_key_for(model_id)
    else:
        model_key, model_id = _default_model()

    if not script and name:
        loaded = store.load_script(name)
        if not loaded:
            return JSONResponse({"error": f"no saved workflow '{name}'"}, status_code=404)
        script = loaded["script"]

    if not script:
        return JSONResponse({"error": "script or name required"}, status_code=400)

    # Parse for phases/name (also a final syntax gate before launch). parse_workflow
    # spawns the Node harness (blocking) — keep it off the event loop.
    from server.workflows.runtime import parse_workflow

    try:
        meta = await asyncio.to_thread(parse_workflow, script)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    run_id = manager.start_run(
        script,
        args=args,
        session_id=session_id,
        model_key=model_key,
        model_id=model_id,
        budget_usd=budget_usd,
        phases=meta.get("phases", []),
        name=name or meta.get("name", ""),
    )
    return {"run_id": run_id, "status": "running"}


@router.post("/runs/{run_id}/stop")
async def stop_run(run_id: str):
    ok = await manager.stop_run(run_id)
    return {"stopped": ok}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    """SSE stream of live events for one run (re-attachable after reload)."""
    from server.agents.event_bus import event_bus

    queue = event_bus.subscribe(f"workflow:{run_id}")

    async def gen():
        # Prime with the current snapshot so a late subscriber isn't blank.
        snap = manager.get_run(run_id)
        if snap:
            yield f"data: {json.dumps({'type': 'snapshot', 'run': snap})}\n\n"
            # If the run already finished before this subscription, its completion
            # event fired earlier and will never arrive — end the stream now
            # instead of hanging until the client disconnects.
            if snap.get("status") in ("done", "failed", "stopped", "stale"):
                event_bus.unsubscribe(f"workflow:{run_id}", queue)
                return
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n"
                    if _is_run_terminal(ev):
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            event_bus.unsubscribe(f"workflow:{run_id}", queue)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/saved")
async def list_saved():
    return {"saved": store.list_scripts()}


@router.get("/saved/{name}")
async def get_saved(name: str):
    loaded = store.load_script(name)
    if not loaded:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "name": name,
        "script": loaded["script"],
        "meta": loaded["meta"],
        "trusted": loaded["trusted"],
    }


@router.post("/saved/{name}/approve")
async def approve_saved(name: str):
    if not store.approve_script(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"approved": True}


@router.delete("/saved/{name}")
async def delete_saved(name: str):
    return {"deleted": store.delete_script(name)}
