"""Two medium MCP fixes:

(A) PUT /api/mcp/servers/{name} (mcp_update_server) must preserve the persisted
    `enabled` flag when it rewrites a server entry. Rebuilding it as
    {command, args, env} only dropped the flag; load_config()'s backfill then
    persisted enabled=false, silently unticking an enabled server on every edit
    or rename (its tools vanished and call_tool returned "... is disabled").

(B) get_bedrock_tools must never advertise a server whose persisted `enabled`
    flag is off, even when a (deprecated) per-request `enabled_names` override
    asks for it — because call_tool enforces the persisted flag at execution
    time, so advertising a disabled server only yields "disabled" at call time.
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
    persistence only (no real MCP subprocess). The refactored start/stop tasks
    are intentionally not driven here."""
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
    must not silently enable it."""
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


# --- (B) override cannot advertise a persisted-disabled server ----------


def _seed(mgr: MCPManager) -> None:
    for server, tool in [("alpha", "ping"), ("beta", "echo")]:
        key = f"mcp__{server}__{tool}"
        mgr._tools[key] = {
            "server_name": server,
            "mcp_tool": _stub_tool(tool),
            "original_name": tool,
        }


def test_override_cannot_advertise_disabled_server(isolated_mcp):
    """A per-request enabled_names override that names a persisted-disabled
    server must NOT advertise its tools — advertisement is intersected with
    the persisted-enabled set so it matches what call_tool would run."""
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

    # Override asks for both; beta is persisted-off so only alpha advertises.
    both = mgr.get_bedrock_tools(enabled_names={"alpha", "beta"})
    assert {t["name"] for t in both} == {"mcp__alpha__ping"}

    # Asking only for the disabled server yields nothing.
    assert mgr.get_bedrock_tools(enabled_names={"beta"}) == []


def test_override_can_still_narrow(isolated_mcp):
    """The override may only NARROW the persisted set, never widen it: with
    both servers enabled, an override of {alpha} drops beta."""
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

    only_alpha = mgr.get_bedrock_tools(enabled_names={"alpha"})
    assert {t["name"] for t in only_alpha} == {"mcp__alpha__ping"}
