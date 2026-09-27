"""Dispatch for the 14 preview tools.

Read-only tools call straight into server/preview/manager.py and return
plain text. Approval-gated tools return a [WS_APPROVAL] sentinel — the same
idiom terminal_run uses — which server/tool_executor.py picks up and routes
through the ApprovalSpec registered in server/approval/bootstrap.py.

The calling chat's session id rides in the approval payload as
``session_id`` (set last, so a model-supplied key cannot claim another chat):
the preview it starts is recorded as that chat's, and the ownership checks in
manager.py read it back when the action runs. A named preview_start also
carries the launch config it resolves to, pinned here the same way.

Called from an async branch in server/tool_router.py (NOT via the
@register_executor/EXECUTORS sync-dispatch path) because Playwright's async
API must stay bound to the single running event loop; a thread-pool-executed
sync wrapper would risk creating browser objects on a different loop than
the one later used to drive them.
"""

from __future__ import annotations

import json

from server.preview.start_policy import PREVIEW_START, pin_launch_config
from server.preview.tools import APPROVAL_GATED_TOOLS

_APPROVAL_GATED_NAMES = {t["name"] for t in APPROVAL_GATED_TOOLS}


def _clean_input(tool_input: dict) -> dict:
    return {k: v for k, v in tool_input.items() if k not in ("__session_id__", "session_id")}


async def execute_preview_tool(tool_name: str, tool_input: dict) -> str:
    from server.preview import manager as m

    session_id = str(tool_input.get("__session_id__") or "")
    tool_input = _clean_input(tool_input)

    if tool_name in _APPROVAL_GATED_NAMES:
        payload = {"action": tool_name, **tool_input, "session_id": session_id}
        if tool_name == PREVIEW_START:
            # What launch.json resolves to now is what gets decided, shown on
            # the card and run (server/preview/start_policy.py).
            pin_launch_config(payload)
        return f"[WS_APPROVAL]{json.dumps(payload)}"

    if tool_name == "preview_list":
        return m.list_preview_sessions(session_id)
    if tool_name == "preview_logs":
        return await m.preview_logs_text(tool_input)
    if tool_name == "preview_screenshot":
        return await m.preview_screenshot_sentinel(tool_input)
    if tool_name == "preview_console_logs":
        return m.preview_console_text(tool_input)
    if tool_name == "preview_network":
        return m.preview_network_text(tool_input)
    if tool_name == "preview_snapshot":
        return await m.preview_snapshot_text(tool_input)
    if tool_name == "preview_inspect":
        return await m.preview_inspect_text(tool_input)

    return f"Unknown preview tool: {tool_name}"
