"""Blocking hooks: PreToolUse can deny or rewrite a tool call, PostToolUse can
feed context back, Stop can refuse to end a turn. One engine, every model path.

Public surface consumed by the tool executor, the chat route and the
completion gate.
"""

from server.hooks.engine import check_stop_hooks, run_hooks

__all__ = ["check_stop_hooks", "run_hooks"]
