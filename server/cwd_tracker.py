"""
Working directory persistence across shell commands per session.

Appends `; pwd` to commands to capture the final working directory,
then stores it per session so the next command starts from there.
"""

import os
import threading

_lock = threading.Lock()
# session_id -> absolute cwd path
_session_cwd: dict[str, str] = {}


def get_cwd(session_id: str, default: str) -> str:
    """Get the persisted cwd for a session, or default if none."""
    with _lock:
        cwd = _session_cwd.get(session_id)
    if cwd and os.path.isdir(cwd):
        return cwd
    return default


def update_cwd(session_id: str, cwd: str):
    """Store the working directory for a session."""
    with _lock:
        _session_cwd[session_id] = cwd


def clear_session(session_id: str):
    """Remove persisted cwd for a session."""
    with _lock:
        _session_cwd.pop(session_id, None)


def wrap_command_for_cwd(command: str) -> str:
    """Append pwd capture to a command.

    The last line of stdout will be the final working directory.
    """
    return f"{command}\necho __CWD_MARKER__; pwd"


def extract_cwd_from_output(output: str) -> tuple[str, str]:
    """Extract and strip the cwd from command output.

    Returns (clean_output, cwd_path). If marker not found,
    returns (output, "").
    """
    marker = "__CWD_MARKER__"
    idx = output.rfind(marker)
    if idx == -1:
        return output, ""
    clean = output[:idx].rstrip("\n")
    after = output[idx + len(marker) :].strip()
    # pwd output is the first line after marker
    lines = after.splitlines()
    cwd = lines[0].strip() if lines else ""
    return clean, cwd
