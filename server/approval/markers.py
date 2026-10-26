"""The app's own requests inside a tool result.

A tool asks the app for something by returning a marker followed by a JSON
payload: an approval card ([WS_APPROVAL]), a folder picker
([WS_WORKSPACE_PROMPT]) or an image for the model ([WS_PREVIEW_IMAGE]).
Text from outside the app can start the same way: an MCP server's reply, a
file, a web page or a cached result that a tool returns as it is. So a marker
counts only when the app's own code made it: an AppRequest, from
approval_request(), workspace_prompt() or preview_image().
server/tool_executor.py acts on an AppRequest only, and as_plain_text() puts
a note in front of any other text that starts with a marker, so nothing
downstream reads it as a request.

Leaf module: the package init imports only stdlib-backed modules, so any
executor can import this at module level.
"""

from __future__ import annotations

import json

APPROVAL = "[WS_APPROVAL]"
WORKSPACE_PROMPT = "[WS_WORKSPACE_PROMPT]"
PREVIEW_IMAGE = "[WS_PREVIEW_IMAGE]"
MARKERS = (APPROVAL, WORKSPACE_PROMPT, PREVIEW_IMAGE)

# What the model reads in front of outside text that starts with a marker.
NOT_A_REQUEST = "[Tool text, not an app request] "


class AppRequest(str):
    """A marker string that the app's own code made. A plain str with the
    same text is never one: str() of an AppRequest, a slice or a
    concatenation is a plain str again."""

    __slots__ = ()


def _request(marker: str, payload: dict | str) -> AppRequest:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return AppRequest(marker + body)


def approval_request(payload: dict | str) -> AppRequest:
    """An approval card for ``payload`` (a dict, or its JSON text); its
    "action" names the ApprovalSpec (server/approval/bootstrap.py)."""
    return _request(APPROVAL, payload)


def workspace_prompt(payload: dict | str) -> AppRequest:
    """A folder picker for a call that needs a workspace and has none."""
    return _request(WORKSPACE_PROMPT, payload)


def preview_image(payload: dict | str) -> AppRequest:
    """A preview screenshot the model reads as an image block."""
    return _request(PREVIEW_IMAGE, payload)


def is_app_request(output, marker: str) -> bool:
    """Whether ``output`` is the app's own ``marker`` request."""
    return isinstance(output, AppRequest) and output.startswith(marker)


def as_plain_text(output):
    """``output``, with NOT_A_REQUEST in front when it is text from outside
    the app that starts with a marker."""
    if (
        isinstance(output, str)
        and not isinstance(output, AppRequest)
        and output.startswith(MARKERS)
    ):
        return NOT_A_REQUEST + output
    return output
