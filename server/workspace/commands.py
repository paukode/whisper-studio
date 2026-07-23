"""Shell-execution helpers shared by both executors and HTTP routes.

These were extracted because both `_execute_command_directly` (in executors.py)
and `ws_shell_endpoint` (in routes.py) — plus `server/approval/bootstrap.py` —
need the same validation, redirection, truncation, and exit-code interpretation
logic. Living in their own module avoids circular imports between executors
and routes.
"""

import base64
import os
import re
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
    """Check if command needs < /dev/null to prevent stdin hangs."""
    if "< " in command or "<<" in command:
        return False
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


def _validate_command(command: str) -> str | None:
    """Validate command via command_validator module."""
    from server.security.command_validator import validate_command

    return validate_command(command)


def _interpret_exit_code(command: str, exit_code: int) -> str | None:
    """Return human-readable meaning for known command+exitcode pairs."""
    if exit_code == 0:
        return None
    base = command.split()[0].rsplit("/", 1)[-1] if command.strip() else ""
    meanings = _EXIT_CODE_MEANINGS.get(base)
    if meanings and exit_code in meanings:
        return meanings[exit_code]
    return None


_IMAGE_SIGNATURES = {
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}

# Matches a chunk of base64 (at least 100 chars, typical for images)
_BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")


def _detect_image_output(output: str) -> dict | None:
    """Detect base64-encoded image data in command output.

    Returns {"mime_type": ..., "data": ...} if found, else None.
    """
    match = _BASE64_PATTERN.search(output)
    if not match:
        return None
    b64_str = match.group(0)
    try:
        raw = base64.b64decode(b64_str)
    except Exception:
        return None
    for sig, mime in _IMAGE_SIGNATURES.items():
        if raw.startswith(sig):
            return {"mime_type": mime, "data": b64_str}
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
