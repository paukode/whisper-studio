"""Shell-execution helpers for ws_run_command (server/workspace/executors.py)
and the approved command executor (server/approval/executors.py): command
validation, stdin redirection, output truncation and exit-code meanings.
"""

import os
import time

from .paths import DATA_DIR

_SHELL_OUTPUT_MAX = 50_000
_SHELL_OUTPUT_DIR = os.path.join(DATA_DIR, "shell_output")

_EXIT_CODE_MEANINGS = {
    "grep": {1: "no matches found"},
    "egrep": {1: "no matches found"},
    "fgrep": {1: "no matches found"},
    "diff": {1: "files differ"},
    "cmp": {1: "files differ"},
    "test": {1: "condition is false"},
    "curl": {6: "could not resolve host", 7: "failed to connect", 22: "HTTP error returned"},
    "git": {1: "command returned error", 128: "fatal error"},
    "ping": {1: "no reply received", 2: "error"},
}


def _needs_stdin_redirect(command: str) -> bool:
    """Check if command needs < /dev/null to prevent stdin hangs: True unless
    it already redirects input somewhere. Any unquoted, unescaped ``<``
    counts (``<file``, ``< file``, ``<<EOF``, ``<<<word``), so ``wc -l
    <data.csv`` keeps reading its file."""
    in_single = in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and not in_single:
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "<" and not in_single and not in_double:
            return False
        i += 1
    return True


def _apply_stdin_redirect(command: str) -> str:
    """Insert < /dev/null before the first pipe so only the first command gets it.

    For 'cmd1 | cmd2', produces 'cmd1 < /dev/null | cmd2'.
    For 'cmd1', produces 'cmd1 < /dev/null'.
    """
    # Find first unquoted pipe
    in_single = False
    in_double = False
    for i, ch in enumerate(command):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "|" and not in_single and not in_double:
            return command[:i].rstrip() + " < /dev/null " + command[i:]
    return f"{command} < /dev/null"


def _validate_command(command: str, secret_reads: bool = False) -> str | None:
    """Validate command via command_validator module."""
    from server.security.command_validator import validate_command

    return validate_command(command, secret_reads=secret_reads)


def _interpret_exit_code(command: str, exit_code: int) -> str | None:
    """Return human-readable meaning for known command+exitcode pairs."""
    if exit_code == 0:
        return None
    base = command.split()[0].rsplit("/", 1)[-1] if command.strip() else ""
    meanings = _EXIT_CODE_MEANINGS.get(base)
    if meanings and exit_code in meanings:
        return meanings[exit_code]
    return None


_SILENT_COMMANDS = frozenset(
    {
        "mv",
        "cp",
        "rm",
        "mkdir",
        "rmdir",
        "chmod",
        "chown",
        "chgrp",
        "touch",
        "ln",
        "install",
        "unlink",
    }
)


def _is_silent_command(command: str) -> bool:
    """Check if command is typically silent on success."""
    base = command.split()[0].rsplit("/", 1)[-1] if command.strip() else ""
    return base in _SILENT_COMMANDS


def _truncate_shell_output(output: str) -> str:
    """Truncate large output, persisting full version to disk."""
    if len(output) <= _SHELL_OUTPUT_MAX:
        return output
    os.makedirs(_SHELL_OUTPUT_DIR, exist_ok=True)
    filename = f"output_{int(time.time() * 1000)}.txt"
    filepath = os.path.join(_SHELL_OUTPUT_DIR, filename)
    try:
        with open(filepath, "w") as f:
            f.write(output)
    except Exception:
        filepath = "(failed to persist)"
    head = output[:2000]
    tail = output[-2000:]
    return (
        f"{head}\n\n"
        f"... ({len(output)} chars total, truncated) ...\n\n"
        f"{tail}\n"
        f"Full output saved to: {filepath}"
    )
