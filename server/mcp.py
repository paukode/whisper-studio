import asyncio
import json
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

DATA_DIR = data_root()
MCP_CONFIG_PATH = os.path.join(DATA_DIR, "mcp_servers.json")


class MCPManager:
    """Manages connections to MCP servers and exposes their tools."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._tools: dict[str, dict] = {}
        # Serializes mutations across start_server / stop_server / restart so
        # readers (call_tool, get_bedrock_tools, etc.) never see partial state.
        # asyncio.Lock is correct here because everything runs on the FastAPI
        # event loop. Lazy-init so we bind to whichever loop is current.
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def load_config(self) -> dict:
        """Read mcp_servers.json. Backfills `enabled: false` for any server
        missing the flag, so the default at first run is OFF (token cost
        of an MCP server is non-trivial — ~1.5–5k tokens per server in the
        Bedrock tool list — and the user must opt in explicitly)."""
        try:
            with open(MCP_CONFIG_PATH) as f:
                servers = json.load(f).get("servers", {})
        except Exception:
            return {}
        # One-time backfill: any pre-existing server without the field
        # gets enabled=false. Persist back so the file is explicit.
        changed = False
        for _name, conf in servers.items():
            if isinstance(conf, dict) and "enabled" not in conf:
                conf["enabled"] = False
                changed = True
        if changed:
            try:
                self.save_config(servers)
            except Exception as e:
                log.warning("MCP backfill save failed: %s", e)
        return servers

    def save_config(self, servers: dict):
        os.makedirs(os.path.dirname(MCP_CONFIG_PATH), exist_ok=True)
        with open(MCP_CONFIG_PATH, "w") as f:
            json.dump({"servers": servers}, f, indent=2)

    def is_server_enabled(self, name: str) -> bool:
        """Whether a server's tools should be advertised to Bedrock by
        default. Falls through to false when unknown — the caller can still
        force a server on for one request via the per-request override."""
        config = self.load_config()
        entry = config.get(name)
        if not isinstance(entry, dict):
            return False
        return bool(entry.get("enabled", False))

    def globally_enabled_servers(self) -> set[str]:
        """The set of servers currently marked enabled in the config file.
        Used as the default when a chat request omits an `mcp_servers`
        override."""
        config = self.load_config()
        return {
            name
            for name, conf in config.items()
            if isinstance(conf, dict) and bool(conf.get("enabled", False))
        }

    async def start_server(self, name: str, config: dict):
        from mcp import StdioServerParameters

        command = config.get("command", "")
        args = config.get("args", [])
        env_vars = config.get("env", {})

        if not command:
            log.warning("MCP server %s: no command specified", name)
            return

        env = {**os.environ, **env_vars}
        params = StdioServerParameters(command=command, args=args, env=env)

        # The MCP client contexts (stdio_client, ClientSession) open anyio task
        # groups / cancel scopes bound to the task that ENTERS them, so they
        # must be EXITED in that same task. We therefore own each connection's
        # whole lifecycle in one dedicated task (`_serve`): it enters the
        # contexts, publishes the session, waits for a stop signal, then exits
        # the contexts itself. The old code exited them from stop_server's
        # (different) task, which raised "cancel scope in a different task",
        # was swallowed, and leaked the server subprocess.
        loop = asyncio.get_event_loop()
        ready: asyncio.Future = loop.create_future()
        stop_event = asyncio.Event()
        async with self._get_lock():
            task = asyncio.create_task(self._serve(name, params, config, ready, stop_event))
            try:
                await ready
            except Exception as e:
                log.error("MCP server '%s' failed to start: %s", name, e)
                self._sessions[name] = {
                    "status": "error",
                    "error": str(e),
                    "config": config,
                    "tools": {},
                }
                return
            # Record the lifecycle handles so stop_server can signal + join.
            info = self._sessions.get(name)
            if info is not None:
                info["task"] = task

    async def _serve(self, name, params, config, ready, stop_event):
        """Own one MCP connection for its whole lifetime in a single task.

        Enters the client contexts, publishes the session, keeps the read loop
        pumping while awaiting `stop_event`, then exits the contexts here (same
        task) — a clean shutdown that actually terminates the subprocess.
        """
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        try:
            async with (
                stdio_client(params) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()

                tools_result = await session.list_tools()
                server_tools: dict = {}
                tool_registrations: dict = {}
                # Use a double-underscore separator so server name + tool name
                # round-trip without ambiguity (e.g. server "foo_bar" tool "x"
                # becomes "mcp__foo_bar__x", server "foo" tool "bar_x" becomes
                # "mcp__foo__bar_x" — no collision). Pre-collapse any "__"
                # already inside the server name so the separator stays unique.
                safe_server = name.replace("__", "_")
                for tool in tools_result.tools:
                    tool_key = f"mcp__{safe_server}__{tool.name}"
                    server_tools[tool_key] = tool
                    tool_registrations[tool_key] = {
                        "server_name": name,
                        "mcp_tool": tool,
                        "original_name": tool.name,
                    }

                # Feature 19: Also discover MCP resources
                server_resources: dict = {}
                try:
                    resources_result = await session.list_resources()
                    for resource in resources_result.resources:
                        server_resources[str(resource.uri)] = resource
                except Exception as e:
                    log.debug("MCP server '%s' has no resources: %s", name, e)

                # Publish (start_server holds the lock while awaiting `ready`,
                # so no other mutator interleaves with this).
                self._sessions[name] = {
                    "session": session,
                    "config": config,
                    "tools": server_tools,
                    "resources": server_resources,
                    "status": "connected",
                    "stop_event": stop_event,
                }
                self._tools.update(tool_registrations)
                log.info(
                    "MCP server '%s' connected: %d tools, %d resources",
                    name,
                    len(server_tools),
                    len(server_resources),
                )
                if not ready.done():
                    ready.set_result(True)

                # Keep the connection (and its read loop) alive in THIS task
                # until stop_server signals us; then the async-with unwinds here.
                await stop_event.wait()
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            else:
                log.warning("MCP server '%s' connection ended: %s", name, e)
        finally:
            # If we still own the published session (a clean stop_server pop
            # already removed it; an unexpected death did not), drop our
            # registrations so readers never see a dead server.
            info = self._sessions.get(name)
            if info is not None and info.get("stop_event") is stop_event:
                for tool_key in list(info.get("tools", {}).keys()):
                    self._tools.pop(tool_key, None)
                self._sessions.pop(name, None)

    async def stop_server(self, name: str):
        async with self._get_lock():
            info = self._sessions.get(name)
            if not info or info.get("status") != "connected":
                # Still drop a stale error record so the dict matches reality
                self._sessions.pop(name, None)
                return
            # Remove tool registrations FIRST so concurrent readers don't pick
            # a tool whose session is about to disappear.
            for tool_key in list(info.get("tools", {}).keys()):
                self._tools.pop(tool_key, None)
            self._sessions.pop(name, None)
            stop_event = info.get("stop_event")
            task = info.get("task")
        # Signal the owning task to exit its contexts (in its own task) and
        # wait for it to finish so the subprocess is really gone. Done OUTSIDE
        # the lock so a slow shutdown can't wedge other start/stop calls.
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=10)
            except asyncio.TimeoutError:
                log.warning("MCP server '%s' did not shut down in 10s; cancelling", name)
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception as e:
                log.warning("Error stopping MCP server '%s': %s", name, e)

    async def start_all(self):
        config = self.load_config()
        for name, server_config in config.items():
            await self.start_server(name, server_config)

    async def stop_all(self):
        for name in list(self._sessions.keys()):
            await self.stop_server(name)

    async def call_tool(self, tool_key: str, arguments: dict) -> str:
        tool_info = self._tools.get(tool_key)
        if not tool_info:
            for k, v in self._tools.items():
                if self._sanitize_tool_name(k) == tool_key:
                    tool_info = v
                    break
        if not tool_info:
            return f"Unknown MCP tool: {tool_key}"
        server_name = tool_info["server_name"]
        # Enforce the enabled flag at EXECUTION time, not only at
        # advertisement time. A model whose session history contains earlier
        # MCP calls will keep calling those tools by name even when the
        # server's tools are no longer advertised — without this guard,
        # unticking a server in the UI silently did nothing for such
        # sessions ("MCP is always on").
        if not self.is_server_enabled(server_name):
            return (
                f"[MCP] Server '{server_name}' is disabled. Enable it in the "
                "chat toolbar's MCP menu or Settings → MCP to use this tool."
            )
        original_name = tool_info["original_name"]
        session_info = self._sessions.get(server_name)
        if not session_info or session_info.get("status") != "connected":
            return f"MCP server '{server_name}' is not connected"

        session = session_info["session"]
        try:
            result = await session.call_tool(original_name, arguments=arguments)
            parts = []
            for content in result.content:
                if hasattr(content, "text"):
                    parts.append(content.text)
                elif hasattr(content, "data"):
                    parts.append(f"[Binary data: {len(content.data)} bytes]")
                else:
                    parts.append(str(content))
            output = "\n".join(parts)
            if result.isError:
                output = f"[MCP Error] {output}"
            return output
        except Exception as e:
            log.error("MCP tool call error (%s/%s): %s", server_name, original_name, e)
            return f"[MCP Error] {e}"

    def _sanitize_tool_name(self, name: str) -> str:
        return name.replace("-", "_").replace(" ", "_")

    def get_bedrock_tools(self, enabled_names: set[str] | None = None) -> list[dict]:
        """Return the MCP tools to advertise to Bedrock.

        Advertisement is always gated by the persisted per-server `enabled`
        flag in mcp_servers.json:

        - When `enabled_names` is None: advertise exactly the persisted-
          enabled servers. This is the normal path.
        - When `enabled_names` is a (possibly empty) set: it is intersected
          with the persisted-enabled set, so it can only ever NARROW the
          selection, never advertise a server whose persisted flag is off.

        The per-request `enabled_names` override is DEPRECATED. It used to be
        able to advertise persisted-disabled servers for a single turn, but
        call_tool() independently enforces the persisted flag at EXECUTION
        time (is_server_enabled), so every such advertised tool just returned
        "[MCP] Server ... is disabled". The frontend no longer sends it (see
        useMcpToggle.ts: "there is no separate per-turn override anymore").
        Intersecting here keeps advertisement in agreement with what call_tool
        will actually run.
        TODO (follow-up, out of this file's scope): drop the `enabled_names`
        param entirely along with the `mcp_servers` request field in
        server/chat/routes.py and the `mcp_enabled_names` plumbing in
        server/chat/tool_pool.py.

        Each MCP tool definition costs ~150–200 tokens in the Bedrock
        request, so filtering here is the leverage point for the user's
        "MCP off by default, opt-in" request — the connection itself
        stays warm so a server can be re-enabled without paying a cold
        start; only the tool advertisement is gated.
        """
        persisted_enabled = self.globally_enabled_servers()
        if enabled_names is None:
            enabled_names = persisted_enabled
        else:
            # A deprecated override can only narrow, never widen: never
            # advertise a server whose persisted flag is off.
            enabled_names = enabled_names & persisted_enabled

        tools = []
        # Two distinct tool_keys can sanitize to the same Bedrock name
        # (e.g. "mcp__s__web-search" and "mcp__s__web_search" both become
        # "mcp__s__web_search"). Bedrock rejects non-unique tool names, so
        # dedup here. Keep the FIRST — call_tool()'s fallback resolves a
        # sanitized name to the first matching key in the same iteration
        # order, so advertising the first keeps advertisement and dispatch in
        # agreement (the later, shadowed tool was already unreachable).
        seen: set[str] = set()
        for tool_key, tool_info in list(self._tools.items()):
            if tool_info["server_name"] not in enabled_names:
                continue
            safe_name = self._sanitize_tool_name(tool_key)
            if safe_name in seen:
                log.warning(
                    "MCP tool name collision: %r sanitizes to %r, already "
                    "advertised by an earlier tool; skipping. Rename one to "
                    "avoid a shadowed (uncallable) tool.",
                    tool_key,
                    safe_name,
                )
                continue
            seen.add(safe_name)
            mcp_tool = tool_info["mcp_tool"]
            schema = dict(mcp_tool.inputSchema)
            schema.pop("$schema", None)
            schema.pop("additionalProperties", None)
            if "type" not in schema:
                schema["type"] = "object"
            tools.append(
                {
                    "name": safe_name,
                    "description": f"[MCP:{tool_info['server_name']}] {mcp_tool.description or mcp_tool.name}",
                    "input_schema": schema,
                }
            )
        return tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name in self._tools or any(
            self._sanitize_tool_name(k) == tool_name for k in self._tools
        )

    def get_all_resources(self) -> list[dict]:
        """Feature 19: Return all MCP resources for @ mention autocomplete."""
        resources = []
        for server_name, info in self._sessions.items():
            if info.get("status") != "connected":
                continue
            for uri, resource in info.get("resources", {}).items():
                resources.append(
                    {
                        "uri": uri,
                        "name": getattr(resource, "name", str(uri)),
                        "description": getattr(resource, "description", ""),
                        "server": server_name,
                        "mention": f"@mcp:{server_name}/{getattr(resource, 'name', str(uri))}",
                    }
                )
        return resources

    async def read_resource(self, server_name: str, uri: str) -> str:
        """Feature 19: Read a specific MCP resource by URI."""
        info = self._sessions.get(server_name)
        if not info or info.get("status") != "connected":
            return f"[MCP server '{server_name}' not connected]"
        session = info.get("session")
        try:
            result = await session.read_resource(uri)
            parts = []
            for content in result.contents:
                if hasattr(content, "text"):
                    parts.append(content.text)
                elif hasattr(content, "blob"):
                    parts.append(f"[Binary: {len(content.blob)} bytes]")
                else:
                    parts.append(str(content))
            return "\n".join(parts)
        except Exception as e:
            return f"[MCP Resource Error] {e}"

    def get_status(self) -> dict:
        result = {}
        for name, info in self._sessions.items():
            result[name] = {
                "status": info.get("status", "unknown"),
                "tools": list(info.get("tools", {}).keys()),
                "error": info.get("error"),
            }
        return result


# Singleton
mcp_manager = MCPManager()


# --- API Routes ---


@router.get("/servers")
async def mcp_servers_status():
    config = mcp_manager.load_config()
    status = mcp_manager.get_status()
    servers = {}
    for name, conf in config.items():
        s = status.get(name, {"status": "stopped", "tools": [], "error": None})
        servers[name] = {
            "command": conf.get("command", ""),
            "args": conf.get("args", []),
            "env": conf.get("env", {}),
            "enabled": bool(conf.get("enabled", False)),
            "status": s["status"],
            "tools": s.get("tools", []),
            "error": s.get("error"),
        }
    # Tool count when ALL servers are enabled — gives the settings panel
    # a sense of "if I turned everything on, this many tools would be in
    # the Bedrock request". The actual default for a chat request is the
    # subset whose `enabled` flag is true. Counted directly from the warm
    # connections (deduped by sanitized name, as get_bedrock_tools does)
    # rather than via get_bedrock_tools, which now refuses to advertise
    # persisted-disabled servers and so can no longer project the ceiling.
    total_mcp_tools = len({mcp_manager._sanitize_tool_name(k) for k in mcp_manager._tools})
    return {"servers": servers, "total_mcp_tools": total_mcp_tools}


@router.post("/servers")
async def mcp_add_server(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    command = body.get("command", "").strip()
    args = body.get("args", [])
    env = body.get("env", {})
    if not name or not command:
        return Response(
            content=json.dumps({"error": "name and command are required"}),
            status_code=400,
            media_type="application/json",
        )

    config = mcp_manager.load_config()
    config[name] = {"command": command, "args": args, "env": env}
    mcp_manager.save_config(config)

    await mcp_manager.start_server(name, config[name])
    status = mcp_manager.get_status().get(name, {})
    return {
        "name": name,
        "status": status.get("status"),
        "tools": status.get("tools", []),
        "error": status.get("error"),
    }


@router.patch("/servers/{name}")
async def mcp_patch_server(name: str, request: Request):
    """Toggle the per-server `enabled` flag. Distinct from PUT (which
    rewrites the whole server entry); used by the Settings → MCP
    checkbox and the chat-toolbar 'remember this' link."""
    body = await request.json()
    config = mcp_manager.load_config()
    if name not in config:
        return Response(
            content=json.dumps({"error": f"Server '{name}' not found"}),
            status_code=404,
            media_type="application/json",
        )
    if "enabled" in body:
        config[name]["enabled"] = bool(body["enabled"])
    mcp_manager.save_config(config)
    return {"name": name, "enabled": bool(config[name].get("enabled", False))}


@router.delete("/servers/{name}")
async def mcp_remove_server(name: str):
    await mcp_manager.stop_server(name)
    config = mcp_manager.load_config()
    config.pop(name, None)
    mcp_manager.save_config(config)
    return {"removed": name}


@router.post("/servers/{name}/restart")
async def mcp_restart_server(name: str):
    config = mcp_manager.load_config()
    if name not in config:
        return Response(
            content=json.dumps({"error": f"Server '{name}' not found"}),
            status_code=404,
            media_type="application/json",
        )
    await mcp_manager.stop_server(name)
    await mcp_manager.start_server(name, config[name])
    status = mcp_manager.get_status().get(name, {})
    return {
        "name": name,
        "status": status.get("status"),
        "tools": status.get("tools", []),
        "error": status.get("error"),
    }


@router.put("/servers/{name}")
async def mcp_update_server(name: str, request: Request):
    body = await request.json()
    config = mcp_manager.load_config()
    if name not in config:
        return Response(
            content=json.dumps({"error": f"Server '{name}' not found"}),
            status_code=404,
            media_type="application/json",
        )
    new_name = body.get("new_name", "").strip()
    # Read the OLD entry BEFORE popping/renaming so we can carry its state.
    old = config[name]
    command = body.get("command", old.get("command", "")).strip()
    args = body.get("args", old.get("args", []))
    env = body.get("env", old.get("env", {}))
    # Preserve the persisted `enabled` flag. Rebuilding the entry as
    # {command, args, env} only would drop it, and load_config()'s backfill
    # would then persist enabled=false — silently unticking an enabled server
    # on every edit or rename (its tools vanish from the Bedrock request and
    # call_tool returns "[MCP] Server ... is disabled"). The Settings UI uses
    # PUT for both in-place edit and rename, so carry the flag in both branches.
    enabled = bool(old.get("enabled", False))
    await mcp_manager.stop_server(name)
    if new_name and new_name != name:
        config.pop(name)
        config[new_name] = {
            "command": command,
            "args": args,
            "env": env,
            "enabled": enabled,
        }
        target_name = new_name
    else:
        config[name] = {
            "command": command,
            "args": args,
            "env": env,
            "enabled": enabled,
        }
        target_name = name
    mcp_manager.save_config(config)
    await mcp_manager.start_server(target_name, config[target_name])
    status = mcp_manager.get_status().get(target_name, {})
    return {
        "name": target_name,
        "status": status.get("status"),
        "tools": status.get("tools", []),
        "error": status.get("error"),
    }
