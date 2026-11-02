"""
Declarative approval system.

Tools register an `ApprovalSpec` (category, preview kind, summary fn,
executor) by calling `register()` near their definition. The gate in
`tool_approvals` looks the spec up by action name at result time and emits
a generic `approval_request` SSE event the frontend renders via one of
three `preview` renderers.

An executor raises a card with the `[WS_APPROVAL]<json>` marker
(`server/approval/markers.py`). Every per-action conditional (summary,
preview body, payload fields) lives in the `ApprovalSpec`, so the frontend
banner stays generic. Callers import `register` from here and the rest
from the `registry` and `spec` submodules.
"""

from .registry import register

__all__ = ["register"]
