"""MCP enable/disable is live and default-on.

Contracts pinned here:

1. POST /api/mcp/servers persists `enabled: true` and starts the server
   immediately — a newly added server is usable on the next message with
   no app restart.
2. PATCH /api/mcp/servers/{name} {enabled} persists the flag AND applies
   it to the runtime connection (enable connects, disable disconnects),
   returning the server's live status.
3. start_all() starts exactly the enabled servers (missing flag counts
   as enabled), so a disabled server stays stopped across app restarts.
4. The per-turn tool pool (assemble_tool_pool) contains a server's tools
   exactly while it is enabled — flipping the flag changes the pool for
   the very next assembly, which is what makes the toggle immediate.
"""

import asyncio
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from server import mcp as mcp_module
from server.mcp import (
    MCPManager,
    mcp_add_server,
    mcp_patch_server,
    mcp_restart_server,
)


@pytest.fixture
def isolated_mcp(monkeypatch):
    """Point the MCP config path at a temp file so tests don't clobber the
    user's real mcp_servers.json."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mcp_servers.json")
        monkeypatch.setattr(mcp_module, "MCP_CONFIG_PATH", path)
        yield path


class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def _stub_tool(name: str):
    t = MagicMock()
    t.name = name
    t.description = f"stub tool {name}"
    t.inputSchema = {"type": "object", "properties": {}}
    return t


def _bind_recording_manager(monkeypatch) -> tuple[MCPManager, list, list]:
    """Install a fresh manager as the module singleton with start/stop
    replaced by recorders. start_server also publishes a fake connected
    session so the handlers' live-status echo can be asserted."""
    mgr = MCPManager()
    started: list[str] = []
    stopped: list[str] = []

    async def _fake_start(name, config):
        started.append(name)
        mgr._sessions[name] = {
            "status": "connected",
            "config": config,
            "tools": {f"mcp__{name}__ping": _stub_tool("ping")},
        }

    async def _fake_stop(name):
        stopped.append(name)
        mgr._sessions.pop(name, None)

    monkeypatch.setattr(mgr, "start_server", _fake_start)
    monkeypatch.setattr(mgr, "stop_server", _fake_stop)
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)
    return mgr, started, stopped


# --- (1) add: default-enabled + immediate start ---------------------------


def test_add_server_persists_enabled_true_and_starts_immediately(isolated_mcp, monkeypatch):
    _mgr, started, _stopped = _bind_recording_manager(monkeypatch)

    resp = asyncio.run(
        mcp_add_server(_FakeRequest({"name": "alpha", "command": "python3", "args": ["x.py"]}))
    )

    with open(isolated_mcp) as f:
        on_disk = json.load(f)["servers"]
    assert on_disk["alpha"]["enabled"] is True
    assert started == ["alpha"]  # connected as part of the add, no restart
    assert resp["enabled"] is True
    assert resp["status"] == "connected"


# --- (2) PATCH: persist + live apply --------------------------------------


def test_patch_disable_persists_and_stops_the_server(isolated_mcp, monkeypatch):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "c", "args": [], "env": {}, "enabled": True}}},
            f,
        )
    _mgr, started, stopped = _bind_recording_manager(monkeypatch)

    resp = asyncio.run(mcp_patch_server("alpha", _FakeRequest({"enabled": False})))

    with open(isolated_mcp) as f:
        assert json.load(f)["servers"]["alpha"]["enabled"] is False
    assert stopped == ["alpha"]
    assert started == []
    assert resp["enabled"] is False
    assert resp["status"] == "stopped"  # live status echoed back


def test_patch_enable_persists_and_starts_the_server(isolated_mcp, monkeypatch):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "c", "args": [], "env": {}, "enabled": False}}},
            f,
        )
    _mgr, started, stopped = _bind_recording_manager(monkeypatch)

    resp = asyncio.run(mcp_patch_server("alpha", _FakeRequest({"enabled": True})))

    with open(isolated_mcp) as f:
        assert json.load(f)["servers"]["alpha"]["enabled"] is True
    assert started == ["alpha"]
    assert stopped == []
    assert resp["enabled"] is True
    assert resp["status"] == "connected"
    assert resp["tools"] == ["mcp__alpha__ping"]


def test_patch_unknown_server_is_404(isolated_mcp, monkeypatch):
    _bind_recording_manager(monkeypatch)
    resp = asyncio.run(mcp_patch_server("ghost", _FakeRequest({"enabled": True})))
    assert resp.status_code == 404


def test_restart_leaves_a_disabled_server_stopped(isolated_mcp, monkeypatch):
    """Restart must not resurrect a switched-off server (green dot under an
    Off switch)."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "c", "args": [], "env": {}, "enabled": False}}},
            f,
        )
    _mgr, started, stopped = _bind_recording_manager(monkeypatch)

    resp = asyncio.run(mcp_restart_server("alpha"))

    assert stopped == ["alpha"]
    assert started == []
    assert resp["status"] == "stopped"
    assert resp["enabled"] is False


# --- (3) start_all starts exactly the enabled set --------------------------


def test_start_all_skips_disabled_and_treats_missing_flag_as_enabled(isolated_mcp, monkeypatch):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "a", "args": [], "env": {}, "enabled": True},
                    "beta": {"command": "b", "args": [], "env": {}, "enabled": False},
                    "gamma": {"command": "g", "args": [], "env": {}},  # missing flag
                }
            },
            f,
        )
    mgr = MCPManager()
    started: list[str] = []

    async def _fake_start(name, config):
        started.append(name)

    monkeypatch.setattr(mgr, "start_server", _fake_start)
    asyncio.run(mgr.start_all())

    assert sorted(started) == ["alpha", "gamma"]  # beta stays stopped


# --- (4) per-turn pool assembly follows the flag ----------------------------


def test_tool_pool_follows_the_enabled_flag_per_assembly(isolated_mcp, monkeypatch):
    """assemble_tool_pool is called per model request, so a toggle is in
    effect for the very next message: disabled -> the server's tools are
    absent; enabled -> present."""
    from server.chat import tool_pool

    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "x", "args": [], "env": {}, "enabled": False}}},
            f,
        )
    mgr = MCPManager()
    mgr._tools["mcp__alpha__ping"] = {
        "server_name": "alpha",
        "mcp_tool": _stub_tool("ping"),
        "original_name": "ping",
    }
    monkeypatch.setattr(tool_pool, "mcp_manager", mgr)

    names = {t["name"] for t in tool_pool.assemble_tool_pool()}
    assert "mcp__alpha__ping" not in names

    # Flip the persisted flag (what PATCH does) and reassemble: the tool is
    # in the pool for the next message without any process restart.
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "x", "args": [], "env": {}, "enabled": True}}},
            f,
        )
    names = {t["name"] for t in tool_pool.assemble_tool_pool()}
    assert "mcp__alpha__ping" in names
