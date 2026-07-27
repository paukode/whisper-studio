"""Two MCP contracts around the persisted `enabled` flag:

(A) PUT /api/mcp/servers/{name} (mcp_update_server) preserves the persisted
    `enabled` flag when it rewrites a server entry. A rewrite that reduced the
    entry to {command, args, env} would drop the flag, and load_config()'s
    backfill would then persist enabled=false — unticking an enabled server on
    every edit or rename.

(B) get_bedrock_tools advertises exactly the servers whose persisted `enabled`
    flag is on. That is the same set call_tool will run, since call_tool
    enforces the flag at execution time.
"""

import asyncio
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from server import mcp as mcp_module
from server.mcp import MCPManager, mcp_update_server


@pytest.fixture
def isolated_mcp(monkeypatch):
    """Point the MCP config path at a temp file so tests don't clobber the
    user's real mcp_servers.json."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mcp_servers.json")
        monkeypatch.setattr(mcp_module, "MCP_CONFIG_PATH", path)
        yield path


def _stub_tool(name: str):
    """Minimal stand-in for an mcp.types.Tool with the only attributes
    get_bedrock_tools reads."""
    t = MagicMock()
    t.name = name
    t.description = f"stub tool {name}"
    t.inputSchema = {"type": "object", "properties": {}}
    return t


class _FakeRequest:
    """Stand-in for a FastAPI Request exposing just the async .json() the
    handler awaits."""

    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def _bind_fresh_manager(monkeypatch) -> MCPManager:
    """Install a fresh MCPManager as the module singleton the route uses, with
    the connection lifecycle neutered so the PUT handler exercises config
    persistence only (no real MCP subprocess). The start/stop tasks are
    intentionally not driven here."""
    mgr = MCPManager()

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(mgr, "start_server", _noop)
    monkeypatch.setattr(mgr, "stop_server", _noop)
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)
    return mgr


# --- (A) PUT preserves the enabled flag ---------------------------------


def test_put_edit_preserves_enabled_flag(isolated_mcp, monkeypatch):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "old", "args": [], "env": {}, "enabled": True}}},
            f,
        )
    mgr = _bind_fresh_manager(monkeypatch)

    # Edit the command only (no new_name) — the in-place edit branch.
    asyncio.run(mcp_update_server("alpha", _FakeRequest({"command": "new"})))

    with open(isolated_mcp) as f:
        on_disk = json.load(f)["servers"]
    assert on_disk["alpha"]["command"] == "new"
    assert on_disk["alpha"]["enabled"] is True

    # And a subsequent load_config must not backfill it to false (the exact
    # chain the finding describes: dropped flag -> backfill persists false).
    assert mgr.load_config()["alpha"]["enabled"] is True


def test_put_rename_preserves_enabled_flag(isolated_mcp, monkeypatch):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "c", "args": ["--x"], "env": {}, "enabled": True}}},
            f,
        )
    mgr = _bind_fresh_manager(monkeypatch)

    # Rename with no other field changes — the rename branch.
    asyncio.run(mcp_update_server("alpha", _FakeRequest({"new_name": "renamed"})))

    with open(isolated_mcp) as f:
        on_disk = json.load(f)["servers"]
    assert "alpha" not in on_disk
    assert on_disk["renamed"]["command"] == "c"
    assert on_disk["renamed"]["args"] == ["--x"]
    assert on_disk["renamed"]["enabled"] is True
    assert mgr.load_config()["renamed"]["enabled"] is True


def test_put_edit_keeps_disabled_disabled(isolated_mcp, monkeypatch):
    """The flag is preserved in both directions: editing a disabled server
    does not silently enable it."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "c", "args": [], "env": {}, "enabled": False}}},
            f,
        )
    _bind_fresh_manager(monkeypatch)

    asyncio.run(mcp_update_server("alpha", _FakeRequest({"command": "c2"})))

    with open(isolated_mcp) as f:
        on_disk = json.load(f)["servers"]
    assert on_disk["alpha"]["enabled"] is False


# --- (B) advertisement matches what call_tool will run ------------------


def _seed(mgr: MCPManager) -> None:
    for server, tool in [("alpha", "ping"), ("beta", "echo")]:
        key = f"mcp__{server}__{tool}"
        mgr._tools[key] = {
            "server_name": server,
            "mcp_tool": _stub_tool(tool),
            "original_name": tool,
        }


def test_disabled_server_is_not_advertised(isolated_mcp):
    """A server whose persisted `enabled` flag is off contributes no tools,
    while its enabled sibling still does."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}, "enabled": True},
                    "beta": {"command": "y", "args": [], "env": {}, "enabled": False},
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr)

    assert {t["name"] for t in mgr.get_bedrock_tools()} == {"mcp__alpha__ping"}


def test_disabled_server_is_also_refused_at_call_time(isolated_mcp):
    """Advertisement and execution agree: beta's tool is neither advertised
    nor callable, so a stale tool name from session history cannot reach it."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}, "enabled": True},
                    "beta": {"command": "y", "args": [], "env": {}, "enabled": False},
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr)

    result = asyncio.run(mgr.call_tool("mcp__beta__echo", {}))
    assert "disabled" in result
    assert "beta" in result


def test_enabled_servers_are_all_advertised(isolated_mcp):
    """With both flags on, both servers' tools are advertised."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}, "enabled": True},
                    "beta": {"command": "y", "args": [], "env": {}, "enabled": True},
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr)

    assert {t["name"] for t in mgr.get_bedrock_tools()} == {
        "mcp__alpha__ping",
        "mcp__beta__echo",
    }
