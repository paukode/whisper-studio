"""MCP per-server `enabled` flag contract.

These tests pin the behaviour the chat path relies on for token control:
the persisted `enabled` flag in mcp_servers.json is the single gate on a
server's tools. It governs advertisement (get_bedrock_tools) and execution
(call_tool) alike, so a server with the flag off contributes no tools and
answers no calls.
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


def test_get_bedrock_tools_advertises_only_enabled_servers(isolated_mcp):
    """The per-server `enabled` flag decides what is advertised."""
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

    names = [t["name"] for t in mgr.get_bedrock_tools()]
    assert names == ["mcp__alpha__ping"], names


def test_get_bedrock_tools_advertises_nothing_when_all_disabled(isolated_mcp):
    """Every flag explicitly off means no MCP tools at all."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "alpha": {"command": "x", "args": [], "env": {}, "enabled": False},
                    "beta": {"command": "y", "args": [], "env": {}, "enabled": False},
                }
            },
            f,
        )
    mgr = _seed_manager_with_two_servers()

    assert mgr.get_bedrock_tools() == []


def test_get_bedrock_tools_advertises_every_enabled_server(isolated_mcp):
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

    both = mgr.get_bedrock_tools()
    assert {t["name"] for t in both} == {"mcp__alpha__ping", "mcp__beta__echo"}


def test_load_config_backfills_missing_enabled_field(isolated_mcp):
    """A pre-existing config file without `enabled` is backfilled to TRUE
    on first load — a configured server is on unless the user turned it
    off (default-enabled contract)."""
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
    assert config["alpha"]["enabled"] is True
    # And it was persisted back so the file is now explicit.
    with open(isolated_mcp) as f:
        on_disk = json.load(f)
    assert on_disk["servers"]["alpha"]["enabled"] is True


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

    tools = mgr.get_bedrock_tools()
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
                    "beta": {"command": "y", "args": [], "env": {}, "enabled": False},
                }
            },
            f,
        )
    mgr = MCPManager()
    assert mgr.is_server_enabled("alpha") is True
    assert mgr.is_server_enabled("beta") is False
    # A server that isn't configured at all is not enabled.
    assert mgr.is_server_enabled("missing") is False


def test_is_server_enabled_treats_missing_flag_as_enabled(isolated_mcp):
    """A configured server whose entry lacks the flag counts as ENABLED —
    the default-enabled contract for configs written before the flag
    existed (load_config's backfill then makes it explicit)."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"alpha": {"command": "x", "args": [], "env": {}}}},
            f,
        )
    mgr = MCPManager()
    assert mgr.is_server_enabled("alpha") is True
    assert mgr.globally_enabled_servers() == {"alpha"}


def test_call_tool_refuses_disabled_server(isolated_mcp):
    """The enabled flag gates EXECUTION, not just advertisement. A model
    whose session history contains earlier MCP calls keeps calling those
    tools by name even when they're no longer advertised, so the guard at
    call time is what makes unticking a server take effect for such
    sessions."""
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
