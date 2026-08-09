"""Per-server/tool MCP approval granularity.

Two schema additions to mcp_servers.json, tested here:

- `enabled_tools` / `disabled_tools`: allow/deny lists filtering which of a
  server's tools are advertised (get_bedrock_tools) and callable (call_tool).
- `approval_mode` (auto|prompt|writes|approve) + `tool_overrides` (per-tool
  override of the mode): decide whether call_tool() executes a tool
  immediately or defers it to the [WS_APPROVAL] pipeline via the "mcp"
  category resolve_static_decision now understands. The per-server setting
  sits ON TOP of the existing global/category permission system — session
  approvals, permission mode, and custom rules still apply for anything
  short of the "approve" hard floor.

NOTE: as of this writing, server/security/permissions.py has no
`category_modes` concept at all (grepped — only MODE_* global permission
modes exist). The interaction this file actually verifies is therefore with
the existing `mode` / `session_approvals` / custom-rule machinery in
resolve_static_decision, not a `category_modes` feature (there isn't one to
interact with).
"""

import asyncio
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from server import mcp as mcp_module
from server.mcp import MCPManager
from server.security.permissions import (
    MODE_AUTO,
    MODE_BYPASS,
    MODE_DEFAULT,
    resolve_static_decision,
)


@pytest.fixture
def isolated_mcp(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mcp_servers.json")
        monkeypatch.setattr(mcp_module, "MCP_CONFIG_PATH", path)
        yield path


def _stub_tool(name: str):
    t = MagicMock()
    t.name = name
    t.description = f"stub tool {name}"
    t.inputSchema = {"type": "object", "properties": {}}
    return t


def _seed(mgr: MCPManager, server: str, tools: list[str]) -> None:
    for tool in tools:
        key = f"mcp__{server}__{tool}"
        mgr._tools[key] = {
            "server_name": server,
            "mcp_tool": _stub_tool(tool),
            "original_name": tool,
        }


# --- enabled_tools / disabled_tools filter ----------------------------------


def test_disabled_tools_excludes_just_that_tool_from_advertisement(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "disabled_tools": ["delete_all"],
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data", "delete_all"])

    names = {t["name"] for t in mgr.get_bedrock_tools()}
    assert names == {"mcp__srv__get_data"}


def test_enabled_tools_allowlist_restricts_to_only_those_tools(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "enabled_tools": ["get_data"],
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data", "delete_all", "search_stuff"])

    names = {t["name"] for t in mgr.get_bedrock_tools()}
    assert names == {"mcp__srv__get_data"}


def test_disabled_tools_wins_over_enabled_tools_on_overlap(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "enabled_tools": ["get_data", "delete_all"],
                        "disabled_tools": ["delete_all"],
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data", "delete_all"])

    names = {t["name"] for t in mgr.get_bedrock_tools()}
    assert names == {"mcp__srv__get_data"}


def test_no_lists_configured_advertises_everything(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"srv": {"command": "x", "args": [], "env": {}, "enabled": True}}},
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data", "delete_all"])

    names = {t["name"] for t in mgr.get_bedrock_tools()}
    assert names == {"mcp__srv__get_data", "mcp__srv__delete_all"}


def test_disabled_tool_is_also_refused_at_call_time(isolated_mcp):
    """Same defense-in-depth as the server-level enabled flag: a stale
    session history calling a since-disabled tool by name must be refused
    at execution time too, not just left off the advertised catalog."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "disabled_tools": ["delete_all"],
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["delete_all"])

    result = asyncio.run(mgr.call_tool("mcp__srv__delete_all", {}))
    assert "not enabled" in result
    assert "delete_all" in result


# --- approval_mode / tool_overrides change call_tool()'s decision -----------


def test_default_auto_tier_executes_immediately_no_gate(isolated_mcp):
    """Unconfigured servers behave EXACTLY as before this feature: no
    sentinel, straight through to the (absent) session lookup."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {"servers": {"srv": {"command": "x", "args": [], "env": {}, "enabled": True}}},
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])

    result = asyncio.run(mgr.call_tool("mcp__srv__get_data", {}))
    assert not result.startswith("[WS_APPROVAL]")
    assert "not connected" in result


def test_prompt_tier_defers_via_ws_approval_sentinel(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "prompt",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])

    result = asyncio.run(mgr.call_tool("mcp__srv__get_data", {"q": "x"}))
    assert result.startswith("[WS_APPROVAL]")
    payload = json.loads(result[len("[WS_APPROVAL]") :])
    assert payload["action"] == "mcp_tool_call"
    assert payload["server"] == "srv"
    assert payload["tool_name"] == "get_data"
    assert payload["arguments"] == {"q": "x"}


def test_writes_tier_auto_executes_read_looking_tool(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "writes",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])

    result = asyncio.run(mgr.call_tool("mcp__srv__get_data", {}))
    assert not result.startswith("[WS_APPROVAL]")


def test_writes_tier_gates_write_looking_tool(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "writes",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["delete_everything"])

    result = asyncio.run(mgr.call_tool("mcp__srv__delete_everything", {}))
    assert result.startswith("[WS_APPROVAL]")


def test_tool_overrides_wins_over_server_level_approval_mode(isolated_mcp):
    """The more specific setting wins: the server default is "auto" (no
    gate), but this ONE tool is overridden to "approve"."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "auto",
                        "tool_overrides": {"delete_everything": "approve"},
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data", "delete_everything"])

    assert not asyncio.run(mgr.call_tool("mcp__srv__get_data", {})).startswith("[WS_APPROVAL]")
    assert asyncio.run(mgr.call_tool("mcp__srv__delete_everything", {})).startswith("[WS_APPROVAL]")

    assert mgr.get_tool_approval_tier("srv", "get_data") == "auto"
    assert mgr.get_tool_approval_tier("srv", "delete_everything") == "approve"


def test_get_tool_approval_tier_for_tool_name_resolves_via_sanitized_name(isolated_mcp):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "prompt",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])

    assert mgr.get_tool_approval_tier_for_tool_name("mcp__srv__get_data") == "prompt"
    assert mgr.get_tool_approval_tier_for_tool_name("mcp__unknown__tool") is None


def test_approved_tool_call_executes_and_bypasses_re_gating(isolated_mcp):
    """The ApprovalSpec executor calls execute_approved_tool_call directly —
    re-entering call_tool() would just re-emit the same sentinel forever."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "approve",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])

    result = asyncio.run(mgr.execute_approved_tool_call("mcp__srv__get_data", {}))
    assert not result.startswith("[WS_APPROVAL]")
    assert "not connected" in result  # cleared the gate, failed only on the (absent) session


# --- resolve_static_decision: the "mcp" category ----------------------------


def test_approve_tier_is_a_hard_floor_even_under_bypass_mode(isolated_mcp, monkeypatch):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "approve",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)

    decision = resolve_static_decision(
        "mcp__srv__get_data",
        {},
        "mcp",
        session_approvals={"mcp": "allow"},  # blanket session allow — should NOT matter
        mode=MODE_BYPASS,  # bypassPermissions — should NOT matter either
    )
    assert decision == "ask"


def test_prompt_tier_defers_to_standard_pipeline_bypass_still_wins(isolated_mcp, monkeypatch):
    """Unlike "approve", "prompt" does not add its own floor — the ordinary
    resolve_static_decision precedence (bypassPermissions first) governs."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "prompt",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)

    decision = resolve_static_decision(
        "mcp__srv__get_data", {}, "mcp", session_approvals={}, mode=MODE_BYPASS
    )
    assert decision == "allow"


def test_prompt_tier_respects_session_level_deny(isolated_mcp, monkeypatch):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "prompt",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)

    decision = resolve_static_decision(
        "mcp__srv__get_data",
        {},
        "mcp",
        session_approvals={"mcp": "deny"},
        mode=MODE_DEFAULT,
    )
    assert decision == "deny"


def test_prompt_tier_falls_back_to_ask_by_default(isolated_mcp, monkeypatch):
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "prompt",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)

    decision = resolve_static_decision(
        "mcp__srv__get_data", {}, "mcp", session_approvals={}, mode=MODE_DEFAULT
    )
    assert decision == "ask"


def test_mcp_category_defers_to_auto_classifier_when_mode_is_auto(isolated_mcp, monkeypatch):
    """mode == "auto" and nothing resolved it -> None (caller falls back to
    the Haiku classifier), same contract as every other category."""
    with open(isolated_mcp, "w") as f:
        json.dump(
            {
                "servers": {
                    "srv": {
                        "command": "x",
                        "args": [],
                        "env": {},
                        "enabled": True,
                        "approval_mode": "prompt",
                    }
                }
            },
            f,
        )
    mgr = MCPManager()
    _seed(mgr, "srv", ["get_data"])
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)

    decision = resolve_static_decision(
        "mcp__srv__get_data", {}, "mcp", session_approvals={}, mode=MODE_AUTO
    )
    assert decision is None


def test_unconfigured_mcp_tool_name_does_not_crash_resolve_static_decision(
    isolated_mcp, monkeypatch
):
    """A tool_name that isn't a known MCP tool (get_tool_approval_tier_for_
    tool_name returns None) must not blow up the "approve" floor check —
    it simply isn't "approve", so the normal pipeline runs."""
    mgr = MCPManager()  # no tools registered at all
    monkeypatch.setattr(mcp_module, "mcp_manager", mgr)

    decision = resolve_static_decision(
        "mcp__unknown__tool", {}, "mcp", session_approvals={}, mode=MODE_BYPASS
    )
    assert decision == "allow"  # bypassPermissions still applies normally
