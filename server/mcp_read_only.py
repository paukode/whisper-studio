"""Which MCP tools a read-only agent may run.

A read-only agent (explore, plan, a custom type with ``read_only``, or any
agent started from a plan-mode turn) is kept from every tool with a side
effect. Built-in tools say so themselves (server/agents/config.py WRITE_TOOLS
and each executor's registered ``read_only``); an MCP server's tools do not,
and a server can expose a write ("create_issue", "send_email") under any
name. So an MCP tool counts as a read only when it is marked as one, and is
kept from read-only agents otherwise (fail closed):

- the user says so in mcp_servers.json: ``"read_only": true`` on a server
  marks all its tools, ``"read_only_tools": [...]`` names some of them, and
  ``"read_only": false`` stops trusting the server's own hints;
- otherwise the server marks the tool with MCP's ``readOnlyHint`` annotation.
"""

from __future__ import annotations


def is_mcp_tool(tool_name: str) -> bool:
    """True for a connected MCP server's tool. Every MCP tool key is
    ``mcp__<server>__<tool>`` (server/mcp.py), so other names skip the scan."""
    if not tool_name.startswith("mcp__"):
        return False
    from server.mcp import mcp_manager

    return mcp_manager.is_mcp_tool(tool_name)


def is_read_only_mcp_tool(tool_name: str) -> bool:
    """True when the MCP tool ``tool_name`` is marked read-only (see the
    module docstring); False for an unmarked or unknown tool."""
    from server.mcp import mcp_manager

    info = mcp_manager._find_tool_info(tool_name)
    if not info:
        return False
    entry = mcp_manager.load_config().get(info.get("server_name"))
    if isinstance(entry, dict):
        if entry.get("read_only") is True:
            return True
        if info.get("original_name") in (entry.get("read_only_tools") or []):
            return True
        if entry.get("read_only") is False:
            return False
    annotations = getattr(info.get("mcp_tool"), "annotations", None)
    return bool(getattr(annotations, "readOnlyHint", False))
