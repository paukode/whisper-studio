"""MCP connection lifecycle: contexts must be entered AND exited in the same
task, otherwise anyio raises "cancel scope in a different task" on shutdown and
the server subprocess leaks. The manager owns each connection in a dedicated
`_serve` task; stop_server signals it and joins, so teardown runs in that task.
"""

import asyncio

import mcp
import mcp.client.stdio

from server.mcp import MCPManager


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = "desc"
        self.inputSchema = {"type": "object"}


class _FakeToolsResult:
    def __init__(self):
        self.tools = [_FakeTool("ping")]


class _FakeSession:
    def __init__(self, record):
        self._record = record

    async def __aenter__(self):
        self._record["session_enter_task"] = asyncio.current_task()
        return self

    async def __aexit__(self, *exc):
        self._record["session_exit_task"] = asyncio.current_task()
        return False

    async def initialize(self):
        pass

    async def list_tools(self):
        return _FakeToolsResult()

    async def list_resources(self):
        raise RuntimeError("no resources")


class _FakeStdioCtx:
    def __init__(self, record):
        self._record = record

    async def __aenter__(self):
        self._record["ctx_enter_task"] = asyncio.current_task()
        return ("read", "write")

    async def __aexit__(self, *exc):
        self._record["ctx_exit_task"] = asyncio.current_task()
        self._record["subprocess_terminated"] = True
        return False


def _install_fakes(monkeypatch, record):
    monkeypatch.setattr(mcp, "StdioServerParameters", lambda **kw: object(), raising=False)
    monkeypatch.setattr(mcp, "ClientSession", lambda r, w: _FakeSession(record), raising=False)
    monkeypatch.setattr(
        mcp.client.stdio, "stdio_client", lambda params: _FakeStdioCtx(record), raising=False
    )


def test_mcp_lifecycle_exits_contexts_in_entering_task(monkeypatch):
    record: dict = {}
    _install_fakes(monkeypatch, record)
    mgr = MCPManager()
    # Force enabled so tools advertise; connection itself does not consult this.
    monkeypatch.setattr(mgr, "is_server_enabled", lambda name: True)

    async def scenario():
        await mgr.start_server("srv", {"command": "fake"})
        # Connected and the tool is registered.
        assert mgr._sessions["srv"]["status"] == "connected"
        assert "mcp__srv__ping" in mgr._tools
        start_task = record["ctx_enter_task"]
        # Both contexts were entered in the SAME dedicated task.
        assert record["session_enter_task"] is start_task

        await mgr.stop_server("srv")
        # Teardown ran, in the SAME task that entered (the leak-fix invariant).
        assert record.get("subprocess_terminated") is True
        assert record["ctx_exit_task"] is start_task
        assert record["session_exit_task"] is start_task
        # Registrations are gone.
        assert "srv" not in mgr._sessions
        assert "mcp__srv__ping" not in mgr._tools

    asyncio.run(scenario())
