"""A failed MCP connect surfaces through an anyio task group as an
``ExceptionGroup`` whose ``str()`` is the useless "unhandled errors in a
TaskGroup (1 sub-exception)". ``_unwrap_exc`` peels the group down to the real
leaf cause so the UI shows something actionable, and ``start_server`` stores
THAT message (not the group's) when a connection fails.
"""

import asyncio
import builtins

import mcp
import mcp.client.stdio

from server.mcp import MCPManager, _unwrap_exc

# ExceptionGroup is a builtin on 3.11+; reference it through ``builtins`` so
# ruff's py310 target doesn't flag it as an undefined name (this suite runs on
# 3.11+, which is exactly where the anyio task-group failure it models occurs).
_ExceptionGroup = builtins.ExceptionGroup


def test_unwrap_exc_returns_leaf_of_nested_group():
    leaf = FileNotFoundError(2, "No such file or directory: 'agentcore'")
    inner = _ExceptionGroup("inner group", [leaf])
    outer = _ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    msg = _unwrap_exc(outer)
    # The opaque group wrapper is gone …
    assert "TaskGroup" not in msg
    # … and the real cause is what's surfaced.
    assert "No such file or directory" in msg


def test_unwrap_exc_passes_through_plain_exception():
    # Non-group exceptions keep their exact message (prior behavior preserved).
    assert _unwrap_exc(RuntimeError("boom: bad thing happened")) == "boom: bad thing happened"


def test_unwrap_exc_falls_back_to_class_name_when_blank():
    assert _unwrap_exc(RuntimeError("")) == "RuntimeError"


class _RaisingStdioCtx:
    """A stdio_client context whose entry fails the way a real server that
    exits immediately does: an anyio-style ExceptionGroup hiding the cause."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


def test_start_server_stores_unwrapped_connect_error(monkeypatch):
    real_cause = ConnectionError("connection closed by server")
    group = _ExceptionGroup("unhandled errors in a TaskGroup", [real_cause])

    monkeypatch.setattr(mcp, "StdioServerParameters", lambda **kw: object(), raising=False)
    monkeypatch.setattr(
        mcp.client.stdio, "stdio_client", lambda params: _RaisingStdioCtx(group), raising=False
    )
    # Fake transport, so accept the placeholder command without a PATH lookup.
    monkeypatch.setattr(
        MCPManager, "_resolve_command", staticmethod(lambda command, path=None: command)
    )
    mgr = MCPManager()

    async def scenario():
        await mgr.start_server("srv", {"command": "fake"})
        info = mgr._sessions["srv"]
        assert info["status"] == "error"
        # The stored error is the real cause, not the opaque TaskGroup wrapper.
        assert "TaskGroup" not in info["error"]
        assert "connection closed by server" in info["error"]
        # And get_status surfaces the same clean message to the UI.
        status_err = mgr.get_status()["srv"]["error"]
        assert "TaskGroup" not in status_err
        assert "connection closed by server" in status_err

    asyncio.run(scenario())
