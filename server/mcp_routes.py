"""HTTP routes for MCP servers (Settings > MCP, the composer, the import panel).

Every mutation saves mcp_servers.json and then runs MCPManager.reconcile(),
the one place servers start and stop. The response is the server's live
status after that pass, and reconcile's change event tells every other open
window to refetch GET /api/mcp/servers.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.mcp import _VALID_APPROVAL_MODES, mcp_manager

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _clamp_approval_mode(mode) -> str:
    return mode if mode in _VALID_APPROVAL_MODES else "auto"


def _extract_mcp_extra_fields(body: dict, old: dict | None = None) -> dict:
    """Optional per-server fields beyond command/args/env/enabled: the
    remote transport (url, and bearer_token_env_var: the env var NAME
    only, the actual token is read from os.environ at connect time and never
    persisted here), the approval-granularity schema (approval_mode,
    tool_overrides, enabled_tools, disabled_tools) and the per-call bound
    (call_timeout_seconds, see server.mcp.CALL_TIMEOUT_S).

    For an update (`old` given), a field absent from the request body
    carries the previous value forward (as command/args/env already do
    in mcp_update_server), so a partial edit never silently
    resets the rest of the entry.
    """
    old = old or {}
    extra: dict = {}
    url = (body.get("url", old.get("url", "")) or "").strip()
    if url:
        extra["url"] = url
    token_env_var = (
        body.get("bearer_token_env_var", old.get("bearer_token_env_var", "")) or ""
    ).strip()
    if token_env_var:
        extra["bearer_token_env_var"] = token_env_var
    approval_mode = body.get("approval_mode", old.get("approval_mode"))
    if approval_mode is not None:
        extra["approval_mode"] = _clamp_approval_mode(approval_mode)
    tool_overrides = body.get("tool_overrides", old.get("tool_overrides"))
    if isinstance(tool_overrides, dict) and tool_overrides:
        extra["tool_overrides"] = {
            str(k): _clamp_approval_mode(v) for k, v in tool_overrides.items()
        }
    enabled_tools = body.get("enabled_tools", old.get("enabled_tools"))
    if isinstance(enabled_tools, list) and enabled_tools:
        extra["enabled_tools"] = [str(t) for t in enabled_tools]
    disabled_tools = body.get("disabled_tools", old.get("disabled_tools"))
    if isinstance(disabled_tools, list) and disabled_tools:
        extra["disabled_tools"] = [str(t) for t in disabled_tools]
    call_timeout = body.get("call_timeout_seconds", old.get("call_timeout_seconds"))
    if (
        isinstance(call_timeout, (int, float))
        and not isinstance(call_timeout, bool)
        and call_timeout > 0
    ):
        extra["call_timeout_seconds"] = call_timeout
    return extra


def _not_found(name: str) -> Response:
    return Response(
        content=json.dumps({"error": f"Server '{name}' not found"}),
        status_code=404,
        media_type="application/json",
    )


def _editable_config() -> dict | Response:
    """The server list to modify and save back, or a 409 while
    mcp_servers.json is unreadable: saving over it would drop every server
    the broken file still holds."""
    config = mcp_manager.read_config()
    if config is None:
        return Response(
            content=json.dumps(
                {
                    "error": "mcp_servers.json is not a valid server list. Fix or remove "
                    "it before changing servers here."
                }
            ),
            status_code=409,
            media_type="application/json",
        )
    return config


def _live_status(name: str, enabled: bool) -> dict:
    status = mcp_manager.get_status().get(name, {})
    return {
        "name": name,
        "enabled": enabled,
        "status": status.get("status", "stopped"),
        "tools": status.get("tools", []),
        "error": status.get("error"),
    }


def _str_list(value) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _str_map(value) -> dict[str, str]:
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


@router.get("/servers")
async def mcp_servers_status():
    """The one MCP server list every surface reads: the configured servers
    with their live status, the change revision, and pending elicitations.

    mcp_servers.json is edited by hand and by the assistant, so each field is
    coerced to its declared type here: one odd value must not break the list
    for every window."""
    config = mcp_manager.load_config()
    status = mcp_manager.get_status()
    servers = {}
    for name, conf in config.items():
        if not isinstance(conf, dict):
            continue
        s = status.get(name, {"status": "stopped", "tools": [], "error": None})
        servers[name] = {
            "command": str(conf.get("command") or ""),
            "args": _str_list(conf.get("args")),
            "env": _str_map(conf.get("env")),
            "enabled": bool(conf.get("enabled", True)),
            "status": s["status"],
            "tools": s.get("tools", []),
            "error": s.get("error"),
            # Remote transport. bearer_token_env_var is the env var NAME
            # only, never the token value, which is never persisted.
            "url": str(conf.get("url") or ""),
            "bearer_token_env_var": str(conf.get("bearer_token_env_var") or ""),
            "approval_mode": _clamp_approval_mode(conf.get("approval_mode", "auto")),
            "tool_overrides": _str_map(conf.get("tool_overrides")),
            "enabled_tools": _str_list(conf.get("enabled_tools")),
            "disabled_tools": _str_list(conf.get("disabled_tools")),
        }
    return {
        "servers": servers,
        "revision": mcp_manager.revision,
        "config_error": mcp_manager.config_error,
        # Elicitations awaiting a human answer right now, across every
        # connected server; Settings > MCP renders a form for each.
        "pending_elicitations": mcp_manager.get_pending_elicitations(),
    }


@router.post("/servers")
async def mcp_add_server(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    command = body.get("command", "").strip()
    args = body.get("args", [])
    env = body.get("env", {})
    extra = _extract_mcp_extra_fields(body)
    if not name or not (command or extra.get("url")):
        return Response(
            content=json.dumps({"error": "name and either command or url are required"}),
            status_code=400,
            media_type="application/json",
        )

    config = _editable_config()
    if isinstance(config, Response):
        return config
    # New servers are enabled by default and started by the reconcile below,
    # so they are usable on the very next message with no app restart.
    config[name] = {"command": command, "args": args, "env": env, "enabled": True, **extra}
    mcp_manager.save_config(config)
    await mcp_manager.reconcile()
    return _live_status(name, True)


@router.patch("/servers/{name}")
async def mcp_patch_server(name: str, request: Request):
    """Toggle the per-server `enabled` flag AND apply it live: enabling
    connects the server, disabling disconnects it, so the change is real
    for the next message with no app restart. Distinct from PUT (which
    rewrites the whole server entry)."""
    body = await request.json()
    config = _editable_config()
    if isinstance(config, Response):
        return config
    if name not in config:
        return _not_found(name)
    if "enabled" in body:
        config[name]["enabled"] = bool(body["enabled"])
    mcp_manager.save_config(config)
    # A failed connect is not an HTTP error: the flag IS persisted, and the
    # caller gets the live status ("error" + message) to surface in the UI.
    await mcp_manager.reconcile()
    return _live_status(name, bool(config[name].get("enabled", True)))


@router.delete("/servers/{name}")
async def mcp_remove_server(name: str):
    config = _editable_config()
    if isinstance(config, Response):
        return config
    config.pop(name, None)
    mcp_manager.save_config(config)
    await mcp_manager.reconcile()
    return {"removed": name}


@router.post("/servers/{name}/restart")
async def mcp_restart_server(name: str):
    config = mcp_manager.load_config()
    if name not in config:
        return _not_found(name)
    # Reconcile starts it again only if it is enabled: a Restart that
    # reconnected a disabled server would show a green dot under an Off
    # switch. This is also how a server that failed to start is retried.
    await mcp_manager.stop_server(name)
    await mcp_manager.reconcile()
    return _live_status(name, bool(config[name].get("enabled", True)))


@router.put("/servers/{name}")
async def mcp_update_server(name: str, request: Request):
    body = await request.json()
    config = _editable_config()
    if isinstance(config, Response):
        return config
    if name not in config:
        return _not_found(name)
    new_name = body.get("new_name", "").strip()
    old = config[name]
    command = body.get("command", old.get("command", "")).strip()
    args = body.get("args", old.get("args", []))
    env = body.get("env", old.get("env", {}))
    # Preserve the persisted `enabled` flag. Rebuilding the entry as
    # {command, args, env} only would drop it, silently flipping the
    # server's state on every edit or rename. The Settings UI uses PUT for
    # both in-place edit and rename (missing flag counts as enabled,
    # matching load_config's backfill).
    enabled = bool(old.get("enabled", True))
    extra = _extract_mcp_extra_fields(body, old)
    target_name = new_name if new_name and new_name != name else name
    if target_name != name:
        config.pop(name)
    config[target_name] = {
        "command": command,
        "args": args,
        "env": env,
        "enabled": enabled,
        **extra,
    }
    mcp_manager.save_config(config)
    # A rename stops the old name and starts the new one; an edit restarts
    # the server only when its launch settings changed. A disabled server
    # stays stopped through edits and renames.
    await mcp_manager.reconcile()
    return _live_status(target_name, enabled)
