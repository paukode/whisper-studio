"""Executor METADATA registration for the 14 live-preview tools.

These decorated functions are never actually invoked — dispatch happens in
server/tool_router.py's async preview branch (server/preview/router.py),
which intercepts before the generic EXECUTORS fallback, because Playwright's
async API must stay bound to the single running event loop rather than a
thread-pool worker. @register_executor is used here purely so
is_read_only()/is_concurrent_safe() (server/executors/__init__.py) have
the right per-tool classification for permission-mode and batch-concurrency
decisions. If one of these stubs is ever actually called, it means the
tool_router.py branch was bypassed — a routing regression, not normal use.
"""

from server.executors import register_executor

_UNREACHABLE = "Routing error: preview tools must dispatch via tool_router.py's async branch, not the executor registry."


@register_executor("preview_list", read_only=True, concurrent_safe=True)
def _meta_preview_list(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_logs", read_only=True, concurrent_safe=True)
def _meta_preview_logs(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_screenshot", read_only=True, concurrent_safe=True)
def _meta_preview_screenshot(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_console_logs", read_only=True, concurrent_safe=True)
def _meta_preview_console_logs(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_network", read_only=True, concurrent_safe=True)
def _meta_preview_network(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_snapshot", read_only=True, concurrent_safe=True)
def _meta_preview_snapshot(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_inspect", read_only=True, concurrent_safe=True)
def _meta_preview_inspect(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_start", read_only=False, concurrent_safe=False)
def _meta_preview_start(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_stop", read_only=False, concurrent_safe=False)
def _meta_preview_stop(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_navigate", read_only=False, concurrent_safe=False)
def _meta_preview_navigate(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_click", read_only=False, concurrent_safe=False)
def _meta_preview_click(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_fill", read_only=False, concurrent_safe=False)
def _meta_preview_fill(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_eval", read_only=False, concurrent_safe=False, destructive=True)
def _meta_preview_eval(tool_input, transcript, current_attachments):
    return _UNREACHABLE


@register_executor("preview_resize", read_only=False, concurrent_safe=False)
def _meta_preview_resize(tool_input, transcript, current_attachments):
    return _UNREACHABLE
