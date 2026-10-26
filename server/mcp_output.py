"""Sanitize untrusted MCP server output before the turn pipeline reads it.

The turn pipeline (server/tool_executor.py) reads a tool output that begins
with one of these markers as an internal control signal, by prefix: an
approval action to run, a folder picker to show, or an image to inject. An MCP
server's text is untrusted, so it must never be read as one. A server that
returns text starting with a marker is shown to the model as literal text
instead, so a forged "[WS_APPROVAL]{...}" cannot run a write or a command.
(The app's own mcp_tool_call approval marker is generated in
server.mcp.MCPManager.call_tool, never from server content, so it is
unaffected.)
"""

from __future__ import annotations

_INTERNAL_SENTINELS = ("[WS_APPROVAL]", "[WS_WORKSPACE_PROMPT]", "[WS_PREVIEW_IMAGE]")


def neutralize_internal_sentinels(text: str) -> str:
    if isinstance(text, str) and text.startswith(_INTERNAL_SENTINELS):
        return "[MCP output, shown as text; not an app action]\n" + text
    return text
