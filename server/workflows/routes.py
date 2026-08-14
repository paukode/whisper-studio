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
async def list_runs(session_id: str = "", limit: int = 50):
    """Runs, newest first — app-wide by default.

    The module docstring promised this from the start but the route was never
    written, so a launched run was visible ONLY as the inline card in the chat
    that started it: nothing listed runs, and the Settings panel (saved scripts)
    stayed empty no matter how many ran. App-wide rather than session-scoped by
    default because someone asking "what have my workflows been doing" is not
    thinking per-session; pass ``session_id`` for the scoped view.
    """
    runs = manager.list_runs(session_id or None, limit=max(1, min(limit, 200)))
    return {"runs": runs}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = manager.get_run(run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    return run


@router.post("/runs")
async def launch_run(request: Request):
    """Launch a run from the approval card (new script) or by saved name.

    ``trust: true`` is the card's "Always allow this workflow" checkbox: the
    approved script is saved under its (slugified) name with its hash recorded
    as trusted — the same state Settings > Workflows' trust toggle writes, so
    future runs by name skip the card until the script changes."""
    body = await request.json()
    script = (body.get("script") or "").strip()
    name = (body.get("name") or "").strip()
    trust = bool(body.get("trust"))
    session_id = body.get("session_id", "")
    args = body.get("args")
    budget_tokens = body.get("budget_tokens")
    if budget_tokens is None:
        from server.workflows.runtime import DEFAULT_WORKFLOW_BUDGET_TOKENS

        budget_tokens = DEFAULT_WORKFLOW_BUDGET_TOKENS
    # Honor the session's model, given either way round: the approval card
    # echoes back the `model_id` the tool put on the preview, while the slash
    # command knows only the config KEY (the frontend never sees Bedrock ids).
    # Fall back to the configured default only when the caller supplied neither.
    model_id = (body.get("model_id") or "").strip()
    model_key = (body.get("model_key") or "").strip()
    if model_key:
        from server.chat.infra import _get_chat_models

        resolved = (_get_chat_models() or {}).get(model_key)
        if resolved:
            model_id = resolved
        else:
            model_key = ""
    if not model_key and model_id:
        from server.workflows.tools import _model_key_for

        # A miss returns "" — an id we cannot reverse (a workspace-only model, a
        # renamed or removed entry, a stale card) must not resolve its effort
        # against a guessed key. Keep the id the caller gave us and take the
        # configured default key below.
        model_key = _model_key_for(model_id)
        if not model_key:
            model_key = _default_model()[0]
    if not model_key:
        model_key, model_id = _default_model()
    # Same for the effort: the approval card and the /workflow slash command
    # both pass the session's level, so an approved run reasons exactly like an
    # auto-approved one. Omitted ⇒ start_run resolves the configured default.
    effort_label = (body.get("effort_label") or "").strip() or None
    # And the workspace: the card echoes back the exact root it resolved and
    # showed at preview time — re-validated here (it must still exist) but
    # never swapped for a fresh snapshot, or the card would have shown the
    # user one folder and launched against another. A caller with no card
    # value at all (the /workflow slash command) pins to whatever is
    # connected right now, same as the tool path's own no-card-shown case.
    #
    # "The key is present" (even as "") and "the key is absent" are NOT the
    # same thing here: a card previewed with nothing connected legitimately
    # echoes back "" (stay unpinned), which must not be treated as "no card
    # was involved, snapshot whatever is connected now" — that would let a
    # workspace connected during the approval delay silently become the run's
    # root, on a run whose card never showed a folder at all.
    if "workspace_path" in body:
        raw_ws = (body.get("workspace_path") or "").strip()
        workspace_path, ws_error = (
            manager.resolve_workflow_workspace(raw_ws) if raw_ws else (None, "")
        )
    else:
        workspace_path, ws_error = manager.resolve_workflow_workspace(None)
    if ws_error:
        return JSONResponse({"error": ws_error}, status_code=400)

    if not script and name:
        loaded = store.load_script(name)
        if not loaded:
            return JSONResponse({"error": f"no saved workflow '{name}'"}, status_code=404)
        # Trust is enforced HERE, not only in the model-facing workflow_run
        # tool. This route is what /workflow <name> calls, so gating trust only
        # on the tool path let an untrusted saved script run with no preview at
        # all — the one thing "untrusted" is supposed to prevent.
        if not loaded["trusted"]:
            return JSONResponse(
                {
                    "error": f"workflow '{name}' is not trusted",
                    "preview_required": True,
                    "detail": (
                        "Trust it in Settings > Workflows (or approve it once from the "
                        "preview card) before launching it by name."
                    ),
                },
                status_code=403,
            )
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

    trusted_as = None
    if trust:
        slug = store.slugify(name or str(meta.get("name") or ""))
        if slug:
            store.save_script(slug, script, meta, trusted=True)
            trusted_as = slug

    run_id = manager.start_run(
        script,
        args=args,
        session_id=session_id,
        model_key=model_key,
        model_id=model_id,
        effort_label=effort_label,
        workspace_path=workspace_path,
        budget_tokens=budget_tokens,
        phases=meta.get("phases", []),
        name=name or meta.get("name", ""),
    )
    return {"run_id": run_id, "status": "running", "trusted_as": trusted_as}


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


@router.post("/saved/{name}/approve")
async def approve_saved(name: str):
    if not store.approve_script(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"approved": True}


@router.delete("/saved/{name}")
async def delete_saved(name: str):
    return {"deleted": store.delete_script(name)}
