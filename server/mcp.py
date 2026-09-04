import asyncio
import json
import logging
import os
import shutil
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response

from server.infrastructure.paths import data_root
from server.security.sensitive_env import scrub_credential_env

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

DATA_DIR = data_root()
MCP_CONFIG_PATH = os.path.join(DATA_DIR, "mcp_servers.json")

# How long an MCP elicitation (a server asking the human for input mid-call)
# waits for an answer before the callback declines on the human's behalf. This
# bounds TWO things at once: the specific in-flight `call_tool()` invocation
# (and, transitively, whatever awaited it — e.g. a live chat turn's tool
# batch, which will sit idle but connected), and the server's single shared
# receive loop (see `_build_elicitation_callback`'s docstring for why one
# elicitation blocks that server's other traffic until it resolves). A few
# minutes is long enough for a human to notice and answer, short enough that
# a forgotten prompt doesn't wedge either of those indefinitely.
ELICITATION_TIMEOUT_S = 300

# Per-server/tool approval tiers (see MCPManager.get_tool_approval_tier).
#   auto    — no approval needed; the tool executes immediately. Default —
#             an unconfigured server behaves exactly as before this feature.
#   prompt  — every call goes through the normal approval pipeline (category
#             "mcp" in resolve_static_decision): subject to the user's
#             permission mode, session approvals, and custom rules, same as
#             any other approval-gated tool.
#   writes  — tool calls that look read-only (name starts with get_/list_/
#             search_/etc.) auto-execute; anything else goes through the
#             normal approval pipeline like "prompt".
#   approve — every call always asks, and — like the "github-destructive"
#             category — this floor cannot be bypassed by bypassPermissions,
#             a blanket session "yes for all MCP tools", or any other global
#             setting. For a server the user does not fully trust.
_VALID_APPROVAL_MODES = {"auto", "prompt", "writes", "approve"}

# Heuristic used only by the "writes" tier: MCP tools carry no structured
# read/write annotation today, so this is a best-effort name-based guess, not
# a guarantee. Errs toward gating (anything not matching is treated as a
# possible mutation).
_READ_ONLY_TOOL_PREFIXES = (
    "get_",
    "list_",
    "search_",
    "read_",
    "fetch_",
    "query_",
    "find_",
    "describe_",
    "show_",
)


def _looks_like_write_tool(tool_name: str) -> bool:
    return not tool_name.lower().startswith(_READ_ONLY_TOOL_PREFIXES)


def _unwrap_exc(e: BaseException) -> str:
    """Human-meaningful message for a connect failure, unwrapping ExceptionGroups.

    The MCP client contexts (stdio_client / ClientSession) run their transport
    inside an anyio task group. When a connect fails — the command exits
    immediately, a socket is refused, a file is missing — the failure bubbles up
    as a ``BaseExceptionGroup`` whose ``str()`` is the useless
    "unhandled errors in a TaskGroup (1 sub-exception)", hiding the real cause.

    Recurse into the group's first child (groups can nest) until we reach a
    non-group leaf, and return ITS message so the UI shows something actionable
    (e.g. the underlying FileNotFoundError / connection-reset text). A plain
    (non-group) exception is returned verbatim, preserving prior behavior.
    Detection is via the ``exceptions`` attribute so it works on both the 3.11+
    builtin ``ExceptionGroup`` and anyio's backport without a version import.
    """
    seen: set[int] = set()
    cur: BaseException = e
    while id(cur) not in seen:
        seen.add(id(cur))
        children = getattr(cur, "exceptions", None)
        if not children:
            break
        cur = children[0]
    msg = str(cur).strip()
    return msg or cur.__class__.__name__


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
        # Pending elicitations awaiting a human answer, keyed by a generated
        # elicitation_id. Each entry holds the future the elicitation
        # callback is blocked on (see resolve_elicitation / _build_
        # elicitation_callback).
        self._pending_elicitations: dict[str, dict] = {}
        # Best-effort attribution of "which call is this elicitation for":
        # a per-server stack of {"session_id", "agent"} pushed just before
        # `session.call_tool()` and popped after. Most MCP servers (stdio or
        # streamable-http) process one request at a time, so the top of the
        # stack is whichever call is most likely still in flight when an
        # elicitation arrives for that server.
        self._active_calls: dict[str, list[dict]] = {}

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def load_config(self) -> dict:
        """Read mcp_servers.json. Backfills `enabled: true` for any server
        missing the flag: a configured server is ON unless the user turned
        it off. Disabling is a first-class, immediate action (the switch in
        Settings → MCP stops the server at runtime), so opt-out replaces the
        old opt-in default."""
        try:
            with open(MCP_CONFIG_PATH) as f:
                servers = json.load(f).get("servers", {})
        except Exception:
            return {}
        # One-time backfill: any pre-existing server without the field
        # gets enabled=true. Persist back so the file is explicit.
        changed = False
        for _name, conf in servers.items():
            if isinstance(conf, dict) and "enabled" not in conf:
                conf["enabled"] = True
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
        """Whether a server should be running and its tools advertised.
        A configured server missing the flag counts as enabled (the
        backfill makes it explicit on next save); an unknown server is
        not enabled."""
        config = self.load_config()
        entry = config.get(name)
        if not isinstance(entry, dict):
            return False
        return bool(entry.get("enabled", True))

    def globally_enabled_servers(self) -> set[str]:
        """The set of servers currently marked enabled in the config file
        (missing flag counts as enabled)."""
        config = self.load_config()
        return {
            name
            for name, conf in config.items()
            if isinstance(conf, dict) and bool(conf.get("enabled", True))
        }

    @staticmethod
    def _resolve_command(command: str, path: str | None) -> str | None:
        """Absolute path to an MCP server's launch command, or None if missing.
        An absolute command must exist and be executable; a bare name is looked
        up on ``path`` (the enriched PATH)."""
        if not command:
            return None
        if os.path.isabs(command):
            return command if (os.path.isfile(command) and os.access(command, os.X_OK)) else None
        return shutil.which(command, path=path)

    async def start_server(self, name: str, config: dict):
        from mcp import StdioServerParameters

        url = (config.get("url") or "").strip()
        command = config.get("command", "")
        args = config.get("args", [])
        env_vars = config.get("env", {})

        if not url and not command:
            log.warning("MCP server %s: no command or url specified", name)
            return

        params = None
        if not url:
            # A third-party MCP server subprocess must not inherit the host
            # app's credentials (AWS/Bedrock keys, API tokens — canonical list
            # in server/security/sensitive_env.py). Per-server `env` entries
            # from mcp_servers.json are applied AFTER the scrub: configuring
            # one there is the deliberate opt-in that can still forward a
            # credential to that server.
            env = {**scrub_credential_env(os.environ), **env_vars}
            # Resolve the command up front so a missing executable yields an
            # actionable message instead of a raw "[Errno 2] No such file or
            # directory" from the spawn. A GUI-launched .app has a minimal PATH;
            # main.py widens it (enrich_gui_launch_path), and we resolve against
            # that same PATH here.
            resolved = self._resolve_command(command, env.get("PATH"))
            if resolved is None:
                msg = (
                    f'Command "{command}" was not found. Install it and make sure it is on '
                    f"your PATH, or set the full path to the executable in Settings > MCP."
                )
                log.error("MCP server '%s': %s", name, msg)
                async with self._get_lock():
                    self._sessions[name] = {
                        "status": "error",
                        "error": msg,
                        "config": config,
                        "tools": {},
                    }
                return
            params = StdioServerParameters(command=resolved, args=args, env=env)

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
            # Idempotent: enabling an already-connected server (e.g. a
            # double toggle, or enable racing the startup start_all) must
            # not spawn a second _serve task that would orphan the first
            # connection's subprocess when it overwrites _sessions[name].
            existing = self._sessions.get(name)
            if existing is not None and existing.get("status") == "connected":
                return
            task = asyncio.create_task(self._serve(name, params, config, ready, stop_event))
            try:
                await ready
            except Exception as e:
                # Unwrap the anyio ExceptionGroup so the stored error is the real
                # cause, not "unhandled errors in a TaskGroup". This message is
                # what get_status() surfaces to the UI.
                msg = _unwrap_exc(e)
                log.error("MCP server '%s' failed to start: %s", name, msg)
                self._sessions[name] = {
                    "status": "error",
                    "error": msg,
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

        Transport branches on the server config: a `command` uses the local
        stdio transport (a subprocess); a `url` uses the streamable-HTTP
        transport (a remote/hosted MCP server). Both are async context
        managers entered/exited HERE, in this same dedicated task, exactly
        like the stdio-only code before this — the ownership discipline
        described above does not care which transport it is.
        """
        from mcp import ClientSession

        url = (config.get("url") or "").strip()
        if url:
            from mcp.client.streamable_http import streamablehttp_client

            headers = None
            token_env_var = (config.get("bearer_token_env_var") or "").strip()
            if token_env_var:
                # The token itself is never persisted to mcp_servers.json —
                # only the env var NAME is stored in config. Read the actual
                # secret from the environment fresh at connect time.
                token = os.environ.get(token_env_var, "")
                if token:
                    headers = {"Authorization": f"Bearer {token}"}
                else:
                    log.warning(
                        "MCP server '%s': bearer_token_env_var '%s' is not set in the "
                        "environment; connecting without an Authorization header",
                        name,
                        token_env_var,
                    )
            transport_cm = streamablehttp_client(url, headers=headers)
        else:
            from mcp.client.stdio import stdio_client

            transport_cm = stdio_client(params)

        elicitation_callback = self._build_elicitation_callback(name)

        try:
            async with transport_cm as transport_streams:
                # stdio_client yields (read_stream, write_stream);
                # streamablehttp_client yields (read_stream, write_stream,
                # get_session_id). Only the first two are ever needed here.
                read_stream, write_stream = transport_streams[0], transport_streams[1]
                async with ClientSession(
                    read_stream, write_stream, elicitation_callback=elicitation_callback
                ) as session:
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
                # start_server awaits `ready` and unwraps this via _unwrap_exc
                # before storing/surfacing it.
                ready.set_exception(e)
            else:
                log.warning("MCP server '%s' connection ended: %s", name, _unwrap_exc(e))
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
        """Start every ENABLED server. Disabled servers stay stopped —
        the enabled flag now controls the runtime connection itself, not
        just tool advertisement, so a toggle takes effect immediately and
        survives restarts symmetrically."""
        config = self.load_config()
        for name, server_config in config.items():
            if not isinstance(server_config, dict):
                continue
            if not bool(server_config.get("enabled", True)):
                continue
            await self.start_server(name, server_config)

    async def stop_all(self):
        for name in list(self._sessions.keys()):
            await self.stop_server(name)

    def _find_tool_info(self, tool_key: str) -> dict | None:
        tool_info = self._tools.get(tool_key)
        if not tool_info:
            for k, v in self._tools.items():
                if self._sanitize_tool_name(k) == tool_key:
                    tool_info = v
                    break
        return tool_info

    def _tool_is_permitted(self, server_name: str, tool_name: str) -> bool:
        """enabled_tools / disabled_tools allow/deny lists for one server.
        disabled_tools wins on overlap (deny is the stronger signal,
        matching the rest of the permission system's convention). No lists
        configured -> every tool the server advertises is permitted."""
        config = self.load_config()
        entry = config.get(server_name)
        if not isinstance(entry, dict):
            return True
        if tool_name in (entry.get("disabled_tools") or []):
            return False
        allowlist = entry.get("enabled_tools") or []
        if allowlist and tool_name not in allowlist:
            return False
        return True

    def get_tool_approval_tier(self, server_name: str, tool_name: str) -> str:
        """Effective approval tier for one MCP tool: `tool_overrides
        [tool_name]` (per-server config) wins over the server's own
        `approval_mode`, which wins over the default "auto" — an
        unconfigured server behaves exactly as it did before this feature
        (no gating, immediate execution)."""
        config = self.load_config()
        entry = config.get(server_name)
        if not isinstance(entry, dict):
            return "auto"
        overrides = entry.get("tool_overrides")
        tier = overrides.get(tool_name) if isinstance(overrides, dict) else None
        if tier not in _VALID_APPROVAL_MODES:
            tier = entry.get("approval_mode", "auto")
        if tier not in _VALID_APPROVAL_MODES:
            tier = "auto"
        return tier

    def get_tool_approval_tier_for_tool_name(self, tool_name: str) -> str | None:
        """Like get_tool_approval_tier, but looks the (server, original
        tool name) pair up from a model-visible tool name — what
        resolve_static_decision receives. None when `tool_name` isn't a
        known MCP tool (the caller should not treat that as "auto")."""
        info = self._find_tool_info(tool_name)
        if not info:
            return None
        return self.get_tool_approval_tier(info["server_name"], info["original_name"])

    async def call_tool(self, tool_key: str, arguments: dict) -> str:
        tool_info = self._find_tool_info(tool_key)
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
                f"[MCP] Server '{server_name}' is disabled. Enable it in "
                "Settings → MCP to use this tool."
            )
        original_name = tool_info["original_name"]
        if not self._tool_is_permitted(server_name, original_name):
            return (
                f"[MCP] Tool '{original_name}' is not enabled for server "
                f"'{server_name}'. Adjust enabled_tools/disabled_tools in "
                "Settings → MCP to use it."
            )

        # Per-server/tool approval gate. "auto" (the default for any server
        # that hasn't configured this) skips straight to execution — zero
        # behavior change for existing servers. Anything else defers the
        # actual decision to the SAME [WS_APPROVAL] pipeline every other
        # approval-gated tool uses (resolve_static_decision's "mcp" category),
        # so session approvals / permission mode / custom rules all still
        # apply on top of this per-server setting, not instead of it.
        tier = self.get_tool_approval_tier(server_name, original_name)
        if tier != "auto":
            needs_ask = not (tier == "writes" and not _looks_like_write_tool(original_name))
            if needs_ask:
                visible_arguments = {
                    k: v for k, v in arguments.items() if not str(k).startswith("__")
                }
                payload = {
                    "action": "mcp_tool_call",
                    "tool_key": tool_key,
                    "arguments": visible_arguments,
                    "server": server_name,
                    "tool_name": original_name,
                }
                if arguments.get("__agent__"):
                    payload["__agent__"] = True
                return "[WS_APPROVAL]" + json.dumps(payload)

        return await self.execute_approved_tool_call(tool_key, arguments)

    async def execute_approved_tool_call(self, tool_key: str, arguments: dict) -> str:
        """Run an MCP tool call that has already cleared approval — either
        `call_tool` decided the server's tier is "auto"/read-only-"writes",
        or a human approved the `mcp_tool_call` [WS_APPROVAL] action via
        POST /api/approval/execute. This is the ONLY place that actually
        invokes `session.call_tool`; re-entering `call_tool()` from the
        approval executor would just re-emit the same sentinel."""
        tool_info = self._find_tool_info(tool_key)
        if not tool_info:
            return f"Unknown MCP tool: {tool_key}"
        server_name = tool_info["server_name"]
        if not self.is_server_enabled(server_name):
            return (
                f"[MCP] Server '{server_name}' is disabled. Enable it in "
                "Settings → MCP to use this tool."
            )
        original_name = tool_info["original_name"]
        if not self._tool_is_permitted(server_name, original_name):
            return f"[MCP] Tool '{original_name}' is not enabled for server '{server_name}'."
        session_info = self._sessions.get(server_name)
        if not session_info or session_info.get("status") != "connected":
            return f"MCP server '{server_name}' is not connected"

        # Strip the internal dunder keys before they reach the wire — the
        # real MCP server sees only the arguments the model actually passed.
        # __session_id__ / __agent__ (injected by tool_executor.py / the
        # agent runtime for every tool call) are read here to attribute an
        # in-flight elicitation (see _build_elicitation_callback), not
        # forwarded to the tool.
        call_arguments = {k: v for k, v in arguments.items() if not str(k).startswith("__")}
        session_id = arguments.get("__session_id__")
        is_agent = bool(arguments.get("__agent__"))

        session = session_info["session"]
        self._active_calls.setdefault(server_name, []).append(
            {"session_id": session_id, "agent": is_agent}
        )
        try:
            result = await session.call_tool(original_name, arguments=call_arguments)
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
        finally:
            stack = self._active_calls.get(server_name)
            if stack:
                stack.pop()

    def _build_elicitation_callback(self, server_name: str):
        """Build the `elicitation_callback` bound into `ClientSession` for
        one server's connection.

        Task/cancel-scope note: the MCP SDK invokes this callback from
        `ClientSession._received_request`, which runs on the SAME task as
        the session's own receive loop — a task the SDK spawns (via its
        internal task group's `start_soon`) when `ClientSession.__aenter__`
        runs, i.e. inside `_serve`'s already-owned context. We never enter or
        exit a context manager here, and we never touch that task group's
        cancel scope directly — we only `await` a plain `asyncio.Future`
        (resolved later by a completely different task, `resolve_elicitation`,
        via ordinary cross-task asyncio; this app runs one event loop, so no
        thread-hop or extra scope is involved). That keeps this callback
        fully inside the ownership `_serve` already holds: if `_serve`'s own
        contexts get torn down (stop_server, or the whole task cancelled),
        the cancellation reaches this `await` exactly as it would any other
        code running on that task tree, and our `finally` still runs.

        One consequence of running on the shared receive-loop task: while
        this callback is waiting, that server's connection cannot process
        any OTHER incoming message (further elicitations, notifications,
        the response to this very call) until it returns — which is exactly
        why the timeout below is bounded rather than open-ended.
        """

        async def _callback(context, params):
            import mcp.types as types

            # refuse_if_agent-style guard: an in-flight call made on behalf
            # of an unattended subagent must not be able to answer an
            # elicitation itself — there is no human present to consult.
            # Attribution is best-effort (see _active_calls' docstring in
            # __init__): the top of this server's active-call stack is
            # whichever call is most likely still in flight.
            stack = self._active_calls.get(server_name)
            call_ctx = stack[-1] if stack else {}
            if call_ctx.get("agent"):
                log.info(
                    "MCP elicitation from '%s' auto-declined (unattended agent context)",
                    server_name,
                )
                return types.ElicitResult(action="decline")

            elicitation_id = uuid.uuid4().hex
            message = getattr(params, "message", "") or ""
            mode = getattr(params, "mode", "form")
            requested_schema = getattr(params, "requestedSchema", None)
            url = getattr(params, "url", None)

            loop = asyncio.get_running_loop()
            future: asyncio.Future = loop.create_future()
            self._pending_elicitations[elicitation_id] = {
                "server_name": server_name,
                "session_id": call_ctx.get("session_id"),
                "mode": mode,
                "message": message,
                "requested_schema": requested_schema,
                "url": url,
                "future": future,
            }
            log.info(
                "MCP server '%s' elicits input (id=%s, mode=%s): %s",
                server_name,
                elicitation_id,
                mode,
                message[:200],
            )
            try:
                answer = await asyncio.wait_for(future, timeout=ELICITATION_TIMEOUT_S)
            except (TimeoutError, asyncio.TimeoutError):
                log.warning(
                    "MCP elicitation %s from '%s' timed out after %ss; declining",
                    elicitation_id,
                    server_name,
                    ELICITATION_TIMEOUT_S,
                )
                return types.ElicitResult(action="decline")
            finally:
                self._pending_elicitations.pop(elicitation_id, None)

            action = answer.get("action", "decline")
            if action not in ("accept", "decline", "cancel"):
                action = "decline"
            content = answer.get("content") if action == "accept" else None
            return types.ElicitResult(action=action, content=content)

        return _callback

    def resolve_elicitation(
        self, elicitation_id: str, action: str, content: dict | None = None
    ) -> bool:
        """Deliver a human's answer to a pending MCP elicitation, waking the
        callback that's blocked awaiting it (see _build_elicitation_callback).
        Called by the `mcp_elicit_respond` ApprovalSpec executor — the same
        single-hub POST /api/approval/execute every other approval flows
        through.

        This just sets a Future's result from a different task than the one
        awaiting it — ordinary asyncio (this app runs one event loop), NOT an
        attempt to enter/exit anyone else's context manager or cancel scope.
        Returns False if the id is unknown (already answered, timed out, or
        never existed) so the caller can report that instead of silently
        no-op-ing.
        """
        pending = self._pending_elicitations.get(elicitation_id)
        if not pending:
            return False
        future = pending["future"]
        if not future.done():
            future.set_result({"action": action, "content": content})
        return True

    def get_pending_elicitations(self) -> list[dict]:
        """Snapshot of every elicitation currently awaiting a human answer,
        across every connected server. Surfaced via GET /api/mcp/servers so
        Settings → MCP can render a form for each and answer it."""
        return [
            {
                "elicitation_id": eid,
                "server": p["server_name"],
                "session_id": p["session_id"],
                "mode": p["mode"],
                "message": p["message"],
                "requested_schema": p["requested_schema"],
                "url": p["url"],
            }
            for eid, p in self._pending_elicitations.items()
        ]

    def _sanitize_tool_name(self, name: str) -> str:
        return name.replace("-", "_").replace(" ", "_")

    def get_bedrock_tools(self) -> list[dict]:
        """Return the MCP tools to advertise to Bedrock.

        Advertisement is gated by the persisted per-server `enabled` flag in
        mcp_servers.json: exactly the enabled servers contribute tools, which
        matches what call_tool() will accept at execution time (it enforces
        the same flag via is_server_enabled).

        Disabling now also STOPS the server at runtime (its tools leave
        _tools when the connection unwinds), so this flag check is defense
        in depth: it keeps the pool correct in the window before a slow
        disconnect finishes, or if a stop failed and left the connection up.
        Because the pool is assembled per turn, a toggle takes effect on the
        very next message with no restart.
        """
        enabled_servers = self.globally_enabled_servers()

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
            if tool_info["server_name"] not in enabled_servers:
                continue
            if not self._tool_is_permitted(tool_info["server_name"], tool_info["original_name"]):
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


def _clamp_approval_mode(mode) -> str:
    return mode if mode in _VALID_APPROVAL_MODES else "auto"


def _extract_mcp_extra_fields(body: dict, old: dict | None = None) -> dict:
    """Optional per-server fields beyond command/args/env/enabled: the
    remote transport (url, bearer_token_env_var — the env var NAME only;
    the actual token is read from os.environ at connect time and never
    persisted here) and the approval-granularity schema (approval_mode,
    tool_overrides, enabled_tools, disabled_tools).

    For an update (`old` given), a field absent from the request body
    carries the previous value forward — mirroring how command/args/env
    already behave in mcp_update_server — so a partial edit never silently
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
    return extra


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
            "enabled": bool(conf.get("enabled", True)),
            "status": s["status"],
            "tools": s.get("tools", []),
            "error": s.get("error"),
            # Remote transport. bearer_token_env_var is the env var NAME
            # only — never the token value, which is never persisted.
            "url": conf.get("url", ""),
            "bearer_token_env_var": conf.get("bearer_token_env_var", ""),
            "approval_mode": conf.get("approval_mode", "auto"),
            "tool_overrides": conf.get("tool_overrides", {}),
            "enabled_tools": conf.get("enabled_tools", []),
            "disabled_tools": conf.get("disabled_tools", []),
        }
    # Live MCP tool count: disabled servers are disconnected, so _tools
    # holds exactly the connected (enabled) servers' tools. Deduped by
    # sanitized name, matching what get_bedrock_tools advertises.
    total_mcp_tools = len({mcp_manager._sanitize_tool_name(k) for k in mcp_manager._tools})
    return {
        "servers": servers,
        "total_mcp_tools": total_mcp_tools,
        # Elicitations awaiting a human answer right now, across every
        # connected server — Settings → MCP polls this to show a form.
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

    config = mcp_manager.load_config()
    # New servers are enabled by default and started immediately below, so
    # they are usable on the very next message — no app restart.
    config[name] = {"command": command, "args": args, "env": env, "enabled": True, **extra}
    mcp_manager.save_config(config)

    await mcp_manager.start_server(name, config[name])
    status = mcp_manager.get_status().get(name, {})
    return {
        "name": name,
        "enabled": True,
        "status": status.get("status"),
        "tools": status.get("tools", []),
        "error": status.get("error"),
    }


@router.patch("/servers/{name}")
async def mcp_patch_server(name: str, request: Request):
    """Toggle the per-server `enabled` flag AND apply it live: enabling
    connects the server, disabling disconnects it, so the change is real
    for the next message with no app restart. Distinct from PUT (which
    rewrites the whole server entry); the enable/disable switch lives in
    Settings → MCP (the composer no longer has an MCP tick)."""
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
    enabled = bool(config[name].get("enabled", True))
    # Apply the flag to the runtime connection. A failed connect is not an
    # HTTP error: the flag IS persisted, and the caller gets the live
    # status ("error" + message) to surface in the UI.
    if enabled:
        await mcp_manager.start_server(name, config[name])
    else:
        await mcp_manager.stop_server(name)
    status = mcp_manager.get_status().get(name, {})
    return {
        "name": name,
        "enabled": enabled,
        "status": status.get("status", "stopped"),
        "tools": status.get("tools", []),
        "error": status.get("error"),
    }


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
    # A disabled server must stay stopped: with disabled == disconnected,
    # a Restart that reconnected it would show a green dot under an Off
    # switch. Restart only cycles servers that are enabled.
    if bool(config[name].get("enabled", True)):
        await mcp_manager.start_server(name, config[name])
    status = mcp_manager.get_status().get(name, {})
    return {
        "name": name,
        "enabled": bool(config[name].get("enabled", True)),
        "status": status.get("status", "stopped"),
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
    # {command, args, env} only would drop it — silently flipping the
    # server's state on every edit or rename. The Settings UI uses PUT for
    # both in-place edit and rename, so carry the flag in both branches
    # (missing flag counts as enabled, matching load_config's backfill).
    enabled = bool(old.get("enabled", True))
    extra = _extract_mcp_extra_fields(body, old)
    await mcp_manager.stop_server(name)
    if new_name and new_name != name:
        config.pop(name)
        config[new_name] = {
            "command": command,
            "args": args,
            "env": env,
            "enabled": enabled,
            **extra,
        }
        target_name = new_name
    else:
        config[name] = {
            "command": command,
            "args": args,
            "env": env,
            "enabled": enabled,
            **extra,
        }
        target_name = name
    mcp_manager.save_config(config)
    # Reconnect with the new config only if the server is enabled — a
    # disabled server stays stopped through edits and renames.
    if enabled:
        await mcp_manager.start_server(target_name, config[target_name])
    status = mcp_manager.get_status().get(target_name, {})
    return {
        "name": target_name,
        "enabled": enabled,
        "status": status.get("status", "stopped"),
        "tools": status.get("tools", []),
        "error": status.get("error"),
    }
