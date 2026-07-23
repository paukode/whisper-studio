"""MCP per-server `enabled` flag + per-request override contract.

These tests pin the behaviour the chat path relies on for token control:
- A server's tools are only advertised to Bedrock when the server is
  enabled (via the persisted flag OR an explicit per-request override).
- An empty `enabled_names` set means "no MCP tools this turn" — distinct
  from `None`, which means "fall back to the persisted flag".
"""

import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from server import mcp as mcp_module
from server.mcp import MCPManager


@pytest.fixture
def isolated_mcp(monkeypatch):
    """Point the MCP config path at a temp file so tests don't clobber the
    user's real mcp_servers.json. Returns the temp path so tests can pre-
    populate it before constructing a fresh manager."""
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


def _seed_manager_with_two_servers() -> MCPManager:
    """Build a manager with two pretend-connected servers and inject their
    tools directly into the internal _tools dict so we can call
    get_bedrock_tools without spinning up real MCP processes."""
    mgr = MCPManager()
    for server, tool in [("alpha", "ping"), ("beta", "echo")]:
        key = f"mcp__{server}__{tool}"
        mgr._tools[key] = {
            "server_name": server,
            "mcp_tool": _stub_tool(tool),
            "original_name": tool,
        }
    return mgr


def test_get_bedrock_tools_filters_by_enabled_names(isolated_mcp):
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
    mgr = _seed_manager_with_two_servers()

    only_alpha = mgr.get_bedrock_tools(enabled_names={"alpha"})
    names = [t["name"] for t in only_alpha]
    assert names == ["mcp__alpha__ping"], names

    nothing = mgr.get_bedrock_tools(enabled_names=set())
    assert nothing == []

    both = mgr.get_bedrock_tools(enabled_names={"alpha", "beta"})
    assert {t["name"] for t in both} == {"mcp__alpha__ping", "mcp__beta__echo"}


def test_get_bedrock_tools_falls_back_to_persisted_enabled(isolated_mcp):
    """When `enabled_names` is None, the per-server `enabled` flag wins."""
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
    mgr = _seed_manager_with_two_servers()

    tools = mgr.get_bedrock_tools()  # enabled_names=None
    names = [t["name"] for t in tools]
    assert names == ["mcp__alpha__ping"], names


def test_load_config_backfills_missing_enabled_field(isolated_mcp):
    """A pre-existing config file without `enabled` should be backfilled
    to false on first load — that's what makes the default off-by-default."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}},  # no enabled
                }
            },
            f,
        )
    mgr = MCPManager()
    config = mgr.load_config()
    assert config["alpha"]["enabled"] is False
    # And it was persisted back so the file is now explicit.
    with open(isolated_mcp) as f:
        on_disk = json.load(f)
    assert on_disk["servers"]["alpha"]["enabled"] is False


def test_globally_enabled_servers_returns_only_enabled(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}, "enabled": True},
                    "beta": {"command": "y", "args": [], "env": {}, "enabled": False},
                    "gamma": {"command": "z", "args": [], "env": {}, "enabled": True},
                }
            },
            f,
        )
    mgr = MCPManager()
    assert mgr.globally_enabled_servers() == {"alpha", "gamma"}


def test_get_bedrock_tools_dedups_sanitized_name_collisions(isolated_mcp):
    """Two tool keys that differ only by `-` vs `_` sanitize to the same
    Bedrock name. get_bedrock_tools must advertise it once (Bedrock rejects
    non-unique names), keeping the first — which is the one call_tool()
    resolves to."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"s": {"command": "x", "args": [], "env": {}, "enabled": True}}},
            f,
        )
    mgr = MCPManager()
    # Insertion order matters: "web-search" is registered first.
    for original in ("web-search", "web_search"):
        key = f"mcp__s__{original}"
        mgr._tools[key] = {
            "server_name": "s",
            "mcp_tool": _stub_tool(original),
            "original_name": original,
        }

    tools = mgr.get_bedrock_tools(enabled_names={"s"})
    names = [t["name"] for t in tools]
    assert names == ["mcp__s__web_search"], names  # deduped, first kept
    # The surviving advertised name resolves back via call_tool's fallback.
    assert mgr._sanitize_tool_name("mcp__s__web-search") == "mcp__s__web_search"


def test_is_server_enabled_checks_the_persisted_flag(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}, "enabled": True},
                }
            },
            f,
        )
    mgr = MCPManager()
    assert mgr.is_server_enabled("alpha") is True
    assert mgr.is_server_enabled("missing") is False


def test_call_tool_refuses_disabled_server(isolated_mcp):
    """The enabled flag must gate EXECUTION, not just advertisement. A model
    whose session history contains earlier MCP calls keeps calling those
    tools by name even when they're no longer advertised — without the
    execution guard, unticking a server silently did nothing for such
    sessions ("MCP is always on")."""
    import asyncio

    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}, "enabled": False},
                }
            },
            f,
        )
    mgr = MCPManager()
    mgr._tools["mcp__alpha__do_thing"] = {
        "server_name": "alpha",
        "mcp_tool": _stub_tool("do_thing"),
        "original_name": "do_thing",
    }
    result = asyncio.run(mgr.call_tool("mcp__alpha__do_thing", {}))
    assert "disabled" in result
    assert "alpha" in result


def test_call_tool_allows_enabled_server_through_to_session_lookup(isolated_mcp):
    """Enabled server passes the guard — it then fails on the (absent)
    session, proving the guard itself isn't blocking enabled servers."""
    import asyncio

    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}, "enabled": True},
                }
            },
            f,
        )
    mgr = MCPManager()
    mgr._tools["mcp__alpha__do_thing"] = {
        "server_name": "alpha",
        "mcp_tool": _stub_tool("do_thing"),
        "original_name": "do_thing",
    }
    result = asyncio.run(mgr.call_tool("mcp__alpha__do_thing", {}))
    assert "not connected" in result
