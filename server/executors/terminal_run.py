"""
terminal_run executor — let the assistant run shell commands.

Two modes:
  - sandbox (default): spawn an ephemeral hidden PTY, run, capture, kill.
                       Invisible to the user. Use for "check / try / probe".
  - visible:           write into the most-recently-active visible PTY
                       session so the user can watch the command stream.

Every call goes through the [WS_APPROVAL] → approval_request pipeline.
The approval card shows the literal command + mode.

Non-interactive only. Commands that wait for stdin (vim, less, ssh password
prompts) get caught by `_LOOKS_INTERACTIVE` and refused at validation, so
they never reach the PTY and can't hang the chat turn.
"""

from __future__ import annotations

import json
import logging
import os
import re

from server.executors import register_executor

log = logging.getLogger("whisper-studio")


_MAX_TIMEOUT_S = 300
_DEFAULT_TIMEOUT_S = 30

# Heuristics for "this command will wait for human input forever". The
# guard is conservative — false positives just force the assistant to
# rephrase; false negatives cost a full timeout window. Anchored at word
# boundary so a path like `/usr/bin/vim-go` doesn't trigger on `vim`.
_INTERACTIVE_WORDS = (
    "vim",
    "vi",
    "nvim",
    "nano",
    "emacs",
    "less",
    "more",
    "most",
    "top",
    "htop",
    "btop",
    "watch",
    "python -i",
    "python3 -i",
    "node -i",
    "irb",
)


def _looks_interactive(command: str) -> tuple[bool, str]:
    """Return (is_interactive, reason). Best-effort string check; not a
    sandbox. The caller has approval-gating anyway."""
    cmd = command.strip()
    if not cmd:
        return True, "empty command"
    # First whitespace-separated token is what the shell would exec.
    first = cmd.split()[0]
    base = os.path.basename(first)
    for word in _INTERACTIVE_WORDS:
        # Match the word at the start of the command or as a standalone
        # token. Avoids matching `vimdiff` against `vim`.
        if base == word or re.search(rf"\b{re.escape(word)}\b", cmd):
            return (
                True,
                f"command appears interactive (matches {word!r}); rephrase as non-interactive",
            )
    # ssh / sudo without explicit non-interactive flags
    if base == "ssh" and "-o BatchMode=yes" not in cmd and "BatchMode=yes" not in cmd:
        return (
            True,
            "ssh without `BatchMode=yes` will hang on password prompt; add `-o BatchMode=yes`",
        )
    if base == "sudo" and not re.search(r"\bsudo\s+-n\b|\bsudo\s+--non-interactive\b", cmd):
        return (
            True,
            "sudo without `-n` will hang on password prompt; use `sudo -n` or run as the privileged user",
        )
    return False, ""


def _resolve_cwd(payload_cwd: str | None) -> str:
    """Resolve the cwd argument: expanduser, fall back to workspace then $HOME."""
    if payload_cwd:
        path = os.path.expanduser(payload_cwd)
        if os.path.isdir(path):
            return path
    from server.workspace import get_workspace_path

    ws = get_workspace_path()
    if ws and os.path.isdir(ws):
        return ws
    return os.path.expanduser("~")


async def do_terminal_run(payload: dict) -> tuple[bool, str]:
    """Approval-gated executor body. Returns (ok, output_or_error) in the
    same (bool, str) shape every other approval spec uses, so bootstrap.py
    can wrap it uniformly.

    `ok` is True for successful execution OR command-failed-but-ran (the
    model needs to see non-zero exits as data, not approval errors). We
    only return ok=False for *system* failures (bad mode, no visible
    session, etc.) — those are conditions the model can correct."""
    from server.terminal import latest_visible_session, run_in_sandbox, run_in_session

    command = (payload.get("command") or "").strip()
    if not command:
        return False, "command is required"

    # Same command-validation gate as the workspace shell — blocks rm -rf /,
    # reads of ~/.ssh / ~/.aws, etc. Applied to BOTH modes so the assistant
    # can't type a dangerous command into the user's visible terminal either.
    from server.security.command_validator import validate_command

    warning = validate_command(command)
    if warning:
        return False, warning

    mode = (payload.get("mode") or "sandbox").strip()
    if mode not in ("sandbox", "visible"):
        return False, f"invalid mode {mode!r} — must be 'sandbox' or 'visible'"

    try:
        timeout = float(payload.get("timeout", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return False, "timeout must be a number (seconds)"
    timeout = max(1.0, min(timeout, float(_MAX_TIMEOUT_S)))

    interactive, why = _looks_interactive(command)
    if interactive:
        return False, why

    cwd = _resolve_cwd(payload.get("cwd"))

    if mode == "sandbox":
        result = await run_in_sandbox(command, cwd=cwd, timeout=timeout)
    else:
        session = latest_visible_session()
        if session is None:
            return False, (
                "No visible terminal session is open. Either open a terminal in the UI "
                "first, or call this tool with mode='sandbox' to run invisibly."
            )
        result = await run_in_session(session, command, timeout=timeout)

    # Format the result for the model: exit code on its own line, then
    # output. Marking the boundary explicitly makes it easier for the
    # model to reason about success/failure separately from the text.
    summary = f"exit_code: {result['exit_code']}"
    if result.get("timed_out"):
        summary = f"TIMED OUT after {timeout:.0f}s; partial output below.\n{summary}"
    output = result.get("output") or "(no output)"
    return True, f"{summary}\n---\n{output}"


@register_executor("terminal_run", read_only=False, concurrent_safe=False)
def _exec_terminal_run(tool_input, transcript, current_attachments):
    """Emit an approval request. Actual command runs only after approval
    (or session-allow via 'Yes, all cli')."""
    tool_input.pop("__session_id__", "")
    command = (tool_input.get("command") or "").strip()
    if not command:
        return "Error: command is required."
    mode = (tool_input.get("mode") or "sandbox").strip()
    payload = json.dumps(
        {
            "action": "terminal_run",
            "command": command,
            "mode": mode,
            "timeout": tool_input.get("timeout", _DEFAULT_TIMEOUT_S),
            "cwd": tool_input.get("cwd") or "",
        }
    )
    return f"[WS_APPROVAL]{payload}"
