"""A tool script may call an MCP tool marked read-only, and skill_list.

The script gate read only the built-in executor registry, so every MCP tool
was refused, also one the read-only agents may already call. A field session
lost five scripts to it (describe_table, introspect_schema, generic_search,
and skill_list). The gate now uses the read-only agents' rule for MCP tools
(server/mcp_read_only.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server.agent_tools.skill_tools  # noqa: F401  (registers skill_list)
from server.mcp import mcp_manager
from server.workflows.runtime import WorkflowRun


class _Hint:
    def __init__(self, read_only: bool):
        self.readOnlyHint = read_only


class _McpTool:
    def __init__(self, name: str, read_only: bool | None = None):
        self.name = name
        self.annotations = _Hint(read_only) if read_only is not None else None


@pytest.fixture
def mcp(monkeypatch):
    """Install fake MCP tools; returns the list of calls that reached the server."""
    calls: list[tuple[str, dict]] = []

    def install(tools: list[_McpTool], config: dict) -> list[tuple[str, dict]]:
        monkeypatch.setattr(
            mcp_manager,
            "_tools",
            {
                f"mcp__srv__{t.name}": {
                    "server_name": "srv",
                    "original_name": t.name,
                    "mcp_tool": t,
                }
                for t in tools
            },
        )
        monkeypatch.setattr(mcp_manager, "load_config", lambda: config)

        async def call_tool(name, args):
            calls.append((name, args))
            return f"rows for {args.get('table')}"

        monkeypatch.setattr(mcp_manager, "call_tool", call_tool)
        return calls

    return install


def _call(name: str, args: dict) -> dict:
    run = WorkflowRun("toolscript-mcp", "", session_id="s1")
    reply: dict = {}

    async def respond(mid, result):
        reply["ok"] = result

    async def error(mid, err_type, message):
        reply["error"] = message

    run._respond = respond
    run._error = error
    asyncio.run(run._handle_tool(1, {"name": name, "args": args}))
    return reply


@pytest.mark.parametrize(
    ("tool", "config"),
    [
        (_McpTool("describe_table", read_only=True), {}),
        (_McpTool("describe_table"), {"srv": {"read_only_tools": ["describe_table"]}}),
        (_McpTool("describe_table"), {"srv": {"read_only": True}}),
    ],
)
def test_an_mcp_tool_marked_read_only_runs(mcp, tool, config):
    calls = mcp([tool], config)
    reply = _call("mcp__srv__describe_table", {"table": "deals"})
    assert reply == {"ok": {"output": "rows for deals"}}
    assert calls == [("mcp__srv__describe_table", {"table": "deals"})]


@pytest.mark.parametrize(
    ("tool", "config"),
    [
        (_McpTool("create_issue"), {}),
        (_McpTool("create_issue", read_only=False), {}),
        # The user stops trusting the server's own marks.
        (_McpTool("create_issue", read_only=True), {"srv": {"read_only": False}}),
    ],
)
def test_an_mcp_tool_not_marked_read_only_is_refused(mcp, tool, config):
    calls = mcp([tool], config)
    reply = _call("mcp__srv__create_issue", {})
    assert "does not mark read-only" in reply["error"]
    assert "mcp_servers.json" in reply["error"]
    assert calls == []


def test_skill_list_runs():
    reply = _call("skill_list", {})
    assert "skills" in json.loads(reply["ok"]["output"])
