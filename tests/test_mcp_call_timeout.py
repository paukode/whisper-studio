"""An MCP tool call that never answers ends with an error result.

Unbounded, one hung MCP server held the whole chat turn: the tool batch waited
on it, the slot keepalive kept the session live, and a message the user sent
meanwhile was queued for a round that never came. The call is bounded, the
result still flows back as an ordinary tool result, the server is asked to
cancel the abandoned request (the result warns that a retry can repeat its
effects), and the per-server override survives an edit from Settings.
"""

import asyncio
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from server import mcp as mcp_module
from server.mcp import MCPManager
from server.mcp_routes import _extract_mcp_extra_fields


@pytest.fixture
def isolated_mcp(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mcp_servers.json")
        monkeypatch.setattr(mcp_module, "MCP_CONFIG_PATH", path)
        with open(path, "w") as f:
            json.dump({"servers": {"srv": {"command": "x", "args": [], "env": {}}}}, f)
        yield path


class _SilentSession:
    """A connected MCP session whose server never replies."""

    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, arguments=None):
        self.calls += 1
        await asyncio.Event().wait()


def _manager(server_config: dict) -> tuple[MCPManager, _SilentSession]:
    mgr = MCPManager()
    tool = MagicMock()
    tool.name = "slow_query"
    mgr._tools["mcp__srv__slow_query"] = {
        "server_name": "srv",
        "mcp_tool": tool,
        "original_name": "slow_query",
    }
    session = _SilentSession()
    mgr._sessions["srv"] = {"status": "connected", "session": session, "config": server_config}
    return mgr, session


def test_a_call_the_server_never_answers_returns_an_error_result(isolated_mcp):
    mgr, session = _manager({"command": "x", "call_timeout_seconds": 0.05})

    async def go():
        return await asyncio.wait_for(
            mgr.execute_approved_tool_call("mcp__srv__slow_query", {"q": 1}), timeout=5
        )

    out = asyncio.run(go())
    assert session.calls == 1
    assert out.startswith("[MCP Error]") and "did not answer" in out
    # The in-flight bookkeeping unwinds with the call.
    assert not mgr._active_calls.get("srv")


def test_the_default_bound_applies_without_an_override(isolated_mcp, monkeypatch):
    monkeypatch.setattr(mcp_module, "CALL_TIMEOUT_S", 0.05)
    mgr, _ = _manager({"command": "x"})

    async def go():
        return await asyncio.wait_for(
            mgr.execute_approved_tool_call("mcp__srv__slow_query", {}), timeout=5
        )

    assert "did not answer" in asyncio.run(go())


def test_a_call_running_when_its_server_stops_ends_at_once(isolated_mcp, monkeypatch):
    """Restart, an edit that restarts the server, or removing it closes the
    connection with the call still pending, and the SDK never answers it:
    the call ends when the server stops, not at the timeout, and no cancel
    goes to a connection that is gone."""
    mgr, session = _manager({"command": "x", "call_timeout_seconds": 60})
    cancels = []

    async def _record_cancel(*args, **kwargs):
        cancels.append(args)
        return True

    monkeypatch.setattr(mcp_module, "_send_cancelled", _record_cancel)

    async def go():
        mgr._sessions["srv"]["stop_event"] = asyncio.Event()
        call = asyncio.create_task(mgr.execute_approved_tool_call("mcp__srv__slow_query", {}))
        await asyncio.sleep(0.05)
        assert session.calls == 1 and not call.done()
        await mgr.stop_server("srv")
        return await asyncio.wait_for(call, timeout=5)

    out = asyncio.run(go())
    assert out.startswith("[MCP Error]")
    assert "was stopped or restarted while slow_query was running" in out
    assert "a retry can repeat its effects" in out
    assert cancels == []
    assert not mgr._active_calls.get("srv")


def test_the_default_leaves_room_for_an_elicitation_first():
    assert mcp_module.CALL_TIMEOUT_S > mcp_module.ELICITATION_TIMEOUT_S


def test_an_edit_from_settings_keeps_the_servers_own_bound():
    old = {"command": "x", "call_timeout_seconds": 1800}
    assert _extract_mcp_extra_fields({"command": "y"}, old)["call_timeout_seconds"] == 1800
    assert "call_timeout_seconds" not in _extract_mcp_extra_fields({"call_timeout_seconds": 0})


def test_an_abandoned_call_is_cancelled_on_the_server(isolated_mcp):
    """The real SDK session over in-memory streams: the timed-out tools/call
    is followed by notifications/cancelled for the same request id, so the
    server stops the work instead of finishing it after the model was told
    the call failed."""
    import anyio
    from mcp import ClientSession

    async def go():
        to_server, server_inbox = anyio.create_memory_object_stream(10)
        to_client, client_inbox = anyio.create_memory_object_stream(10)
        seen: list[dict] = []

        async def _server_that_never_answers():
            async for msg in server_inbox:
                seen.append(msg.message.root.model_dump(by_alias=True, exclude_none=True))
                if len(seen) == 2:
                    return

        async with ClientSession(client_inbox, to_server) as session:
            mgr, _ = _manager({"command": "x", "call_timeout_seconds": 0.05})
            mgr._sessions["srv"]["session"] = session
            server = asyncio.create_task(_server_that_never_answers())
            out = await asyncio.wait_for(
                mgr.execute_approved_tool_call("mcp__srv__slow_query", {"q": 1}), timeout=5
            )
            await asyncio.wait_for(server, 5)
        await to_client.aclose()
        return out, seen

    out, seen = asyncio.run(go())
    call, cancel = seen
    assert call["method"] == "tools/call" and call["params"]["name"] == "slow_query"
    assert cancel["method"] == "notifications/cancelled"
    assert cancel["params"]["requestId"] == call["id"]
    assert "asked to cancel it" in out
    assert "a retry can repeat its effects" in out


def test_a_server_that_cannot_be_told_to_stop_is_named_as_still_running(isolated_mcp):
    mgr, _ = _manager({"command": "x", "call_timeout_seconds": 0.05})

    async def go():
        return await asyncio.wait_for(
            mgr.execute_approved_tool_call("mcp__srv__slow_query", {}), timeout=5
        )

    out = asyncio.run(go())
    assert "may still be running it" in out
    assert "a retry can repeat its effects" in out
