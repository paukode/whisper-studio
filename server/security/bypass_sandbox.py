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

A read-only scope (server/agents/tool_access.py: a read-only agent, or a
call made in plan mode) keeps the sandbox under the Bypass scope, so the
read-only profile still refuses a write the classifier missed.

A card a person approves runs with the scope of the turn that raised it:
the gate stamps ``UNSANDBOXED_KEY`` on the card payload, and the approval
hub (server/approval/spec.py::execute_approved_by_human) reads that stamp,
never the live mode at the time of the click.

``server/sandbox.py`` reads it: under the scope its entry points run the
command with ``/bin/sh -c`` and no profile. The environment is unchanged
(the GitHub token denylist and the scratch package caches stay).
"""

from __future__ import annotations

import contextlib
import contextvars

# The card payload key that carries the raising turn's scope to the approval
# hub. Only the gate sets it; a value the model sends is dropped.
UNSANDBOXED_KEY = "__unsandboxed__"

# What a danger-full-access call gets in a read-only scope under Bypass:
# Bypass shows no card for the escalation, so no person would read it.
HELD_BACK_REFUSAL = (
    "danger-full-access is not permitted in a read-only scope (plan mode or a "
    "read-only agent). Run it from a normal turn."
)

_scope: contextvars.ContextVar[bool] = contextvars.ContextVar("bypass_unsandboxed", default=False)


def active() -> bool:
    """True when the running code belongs to a Bypass turn, so its commands
    run with no OS sandbox.

    A read-only scope keeps the sandbox: a read-only agent (memory, Explore)
    or a plan-mode call relies on the read-only profile to refuse any write
    the command classifier missed, Bypass or not."""
    return _scope.get() and not _read_only()


def held_back() -> bool:
    """True when a Bypass turn's code runs in a read-only scope, so its
    commands keep the sandbox (see ``active``)."""
    return _scope.get() and _read_only()


def _read_only() -> bool:
    from server.agents.tool_access import read_only_scope

    return read_only_scope.get()


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
