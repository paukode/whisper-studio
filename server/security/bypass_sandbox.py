"""Bypass runs commands with no OS sandbox.

The user's rule (2026-11-02): in bypassPermissions every command runs the
way Claude Code's bypass runs it, with no sandbox-exec and no write limits.
Every other permission mode keeps the sandbox unchanged, and so does an
unattended run that no Bypass turn started (cron, a headless run).

The scope is a context variable, so it reaches every command path with no
new argument: ws_run_command and its background shells, terminal_run's
hidden shell, run_python, aws_cli, office_script, hooks, goal gates, and the
shells of the agents and workflows a Bypass turn starts. asyncio tasks,
``asyncio.to_thread`` and the tool router's executor copy the context, so
an agent or a detached shell started under the scope keeps it.

Who sets it:

- ``server/tool_executor.py`` around each tool batch and its result
  processing (``mode == bypassPermissions``, or the scope already on);
- the chat route around its SessionStart and UserPromptSubmit hooks;
- the completion gate around its Stop hooks and quality gates;
- a workflow run, which keeps the value its launcher had;
- the /subagent route, from the live mode.

It only turns on down a chain, never off, except where a run starts that is
not part of the chain: a cron run starts from a context with it off.

``server/sandbox.py`` reads it: under the scope its entry points run the
command with ``/bin/sh -c`` and no profile. The environment is unchanged
(the GitHub token denylist and the scratch package caches stay).
"""

from __future__ import annotations

import contextlib
import contextvars

_scope: contextvars.ContextVar[bool] = contextvars.ContextVar("bypass_unsandboxed", default=False)


def active() -> bool:
    """True when the running code belongs to a Bypass turn, so its commands
    run with no OS sandbox."""
    return _scope.get()


def is_bypass(mode: str | None) -> bool:
    """Whether ``mode`` is bypassPermissions."""
    from server.security.permissions import MODE_BYPASS

    return mode == MODE_BYPASS


@contextlib.contextmanager
def entered(on: bool):
    """Run the block unsandboxed when ``on``. A scope already on stays on."""
    token = _scope.set(bool(on) or _scope.get())
    try:
        yield
    finally:
        _scope.reset(token)


def set_off(context: contextvars.Context) -> None:
    """Turn the scope off in ``context`` (a copied context a new unattended
    run starts from, such as a cron run)."""
    context.run(_scope.set, False)


def set_on(context: contextvars.Context, on: bool) -> None:
    """Set the scope in ``context`` (a copied context a new run starts from)."""
    context.run(_scope.set, bool(on))
