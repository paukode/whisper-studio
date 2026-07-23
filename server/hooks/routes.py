"""HTTP API for the blocking-hooks system (v2).

Replaces the old index-addressed flat API in server/infrastructure/hooks.py.
Hooks are keyed by a stable id; each carries a matcher, timeout, on_error
policy, and enabled flag. Adds a dry-run Test endpoint and the project-hook
trust surface (arbitrary code from a cloned repo stays inert until approved).
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.hooks import config_loader as cfg
from server.hooks.engine import dry_run
from server.hooks.schema import (
    DEFAULT_TIMEOUT,
    HOOK_EVENTS,
    HookDef,
    build_stdin_payload,
    canonical_event,
    serialize_v2,
)

router = APIRouter(prefix="/api/hooks", tags=["hooks"])


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _hook_row(h: HookDef) -> dict:
    return {
        "id": h.id,
        "event": h.event,
        "matcher": h.matcher,
        "command": h.command,
        "timeout": h.timeout,
        "enabled": h.enabled,
        "on_error": h.on_error,
        "source": h.source,
    }


def _workspace() -> str | None:
    try:
        from server.workspace import get_workspace_path

        return get_workspace_path()
    except Exception:
        return None


@router.get("")
async def get_hooks():
    """User hooks (by event) plus the project-hook trust status."""
    by_event = cfg.load_user_hooks()
    ws = _workspace()
    project_status = cfg.project_trust_status(ws)
    project_hooks = cfg.normalize_config(cfg._project_hooks_raw(ws), source="project")
    return {
        "version": 2,
        "available_events": list(HOOK_EVENTS),
        "hooks": {ev: [_hook_row(h) for h in defs] for ev, defs in by_event.items()},
        "project": {
            "workspace": ws,
            "status": project_status,
            "hooks": {ev: [_hook_row(h) for h in defs] for ev, defs in project_hooks.items()},
        },
    }


def _hook_from_body(body: dict, *, hook_id: str = "") -> HookDef | JSONResponse:
    event = canonical_event((body.get("event") or "").strip())
    if event not in HOOK_EVENTS:
        return _err(f"event must be one of: {list(HOOK_EVENTS)}")
    command = (body.get("command") or "").strip()
    if not command:
        return _err("command is required")
    return HookDef(
        event=event,
        command=command,
        matcher=body.get("matcher", body.get("tool", "*")) or "*",
        timeout=int(body.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT),
        enabled=bool(body.get("enabled", True)),
        on_error=body.get("on_error", "ignore"),
        id=hook_id,
        source="user",
    )


@router.post("")
async def add_hook(request: Request):
    body = await request.json()
    hook = _hook_from_body(body)
    if isinstance(hook, JSONResponse):
        return hook
    stored = cfg.upsert_user_hook(hook)
    return {"added": True, "hook": _hook_row(stored)}


@router.put("/{hook_id}")
async def update_hook(hook_id: str, request: Request):
    body = await request.json()
    # An id that doesn't exist yet is an upsert, but reject if the client is
    # clearly editing a hook that was already deleted elsewhere.
    existing_ids = {h.id for defs in cfg.load_user_hooks().values() for h in defs}
    if hook_id not in existing_ids:
        return _err("Not found", 404)
    hook = _hook_from_body(body, hook_id=hook_id)
    if isinstance(hook, JSONResponse):
        return hook
    stored = cfg.upsert_user_hook(hook)
    return {"updated": True, "hook": _hook_row(stored)}


@router.delete("/{hook_id}")
async def delete_hook(hook_id: str):
    if not cfg.delete_user_hook(hook_id):
        return _err("Not found", 404)
    return {"deleted": hook_id}


@router.post("/test")
async def test_hook(request: Request):
    """Dry-run a command against a synthetic payload (the Test button)."""
    body = await request.json()
    command = (body.get("command") or "").strip()
    if not command:
        return _err("command is required")
    event = canonical_event(body.get("event", "PreToolUse"))
    payload = build_stdin_payload(
        event,
        session_id="test-session",
        workspace=_workspace() or "",
        tool_name=body.get("tool_name", "ws_write_file"),
        tool_input=body.get("tool_input", {"path": "/example.txt"}),
        tool_output=body.get("tool_output"),
    )
    # dry_run spawns a sandboxed subprocess (blocking, up to the timeout).
    # Offload it so it never stalls the shared event loop.
    result = await asyncio.to_thread(
        dry_run, command, payload, timeout=int(body.get("timeout", DEFAULT_TIMEOUT) or 10)
    )
    # Surface how the engine would interpret this exit code / stdout.
    decision = "allow"
    reason = ""
    if result["exit_code"] == 2:
        decision, reason = "deny", (result["stderr"].strip() or "Blocked by hook.")
    elif result["exit_code"] == 0 and result["stdout"].strip():
        try:
            ctrl = json.loads(result["stdout"].strip())
            if isinstance(ctrl, dict) and (ctrl.get("decision") or "").lower() == "deny":
                decision, reason = "deny", ctrl.get("reason", "")
        except (ValueError, TypeError):
            pass
    elif result["exit_code"] not in (0, 2):
        decision, reason = "error", f"exit {result['exit_code']}"
    return {**result, "decision": decision, "reason": reason, "payload": payload}


@router.post("/project/approve")
async def approve_project(request: Request):
    """Trust the current workspace's project hooks (mirrors trusted skills)."""
    ws = _workspace()
    if not ws:
        return _err("No workspace connected")
    if not cfg.approve_project_hooks(ws):
        return _err("No project hooks to approve")
    return {"approved": True, "status": cfg.project_trust_status(ws)}


@router.post("/project/revoke")
async def revoke_project(request: Request):
    """Drop trust for the current workspace's project hooks (they go inert)."""
    ws = _workspace()
    if not ws:
        return _err("No workspace connected")
    if not cfg.revoke_project_hooks(ws):
        return _err("No trusted project hooks to revoke")
    return {"revoked": True, "status": cfg.project_trust_status(ws)}


@router.get("/export")
async def export_hooks():
    """The raw v2 document (for backup / copy between machines)."""
    return serialize_v2(cfg.load_user_hooks())
