"""
Declarative approval system.

Tools register an `ApprovalSpec` (category, preview kind, summary fn,
executor) by calling `register()` near their definition. `tool_executor`
looks the spec up by tool name (or action name) at result time and emits
a generic `approval_request` SSE event the frontend renders via one of
four `preview` renderers.

Replaces the old `[WS_APPROVAL]<json>` magic-string protocol whose
per-action conditionals leaked into the frontend banner and were the
proximate cause of "approval emitted but UI didn't render" bugs.
"""

from .registry import all_categories, all_specs, get, register
from .spec import ApprovalOutcome, ApprovalSpec

__all__ = [
    "ApprovalSpec",
    "ApprovalOutcome",
    "register",
    "get",
    "all_specs",
    "all_categories",
]
