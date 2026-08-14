"""
Working directory persistence across shell commands per session.

Appends `; pwd` to commands to capture the final working directory,
then stores it per session so the next command starts from there.
"""

import os
import threading

_lock = threading.Lock()
# tracking key -> absolute cwd path. The key is session_id alone outside
# worktree isolation; see _key() for what changes under it.
_session_cwd: dict[str, str] = {}


def _active_override() -> str | None:
    """The current task's worktree override, or None outside isolation.
    Lazy-imported: cwd_tracker is a small leaf module and workspace.state
    pulls in a much larger graph."""
    try:
        from server.workspace.state import get_workspace_override

        return get_workspace_override()
    except Exception:  # noqa: BLE001 — never let tracking break a command
        return None


def _key(session_id: str, override: str | None) -> str:
    """Fold the active override into the tracking key.

    Without this, a worktree-isolated agent shares its cwd slot with the
    parent session (and with every sibling isolated agent running the same
    session_id): a cwd the user or another agent left behind in the ORIGINAL
    checkout would silently become the isolated agent's effective_cwd, and an
    isolated agent that itself cd's around would leak that path back out.
    Every worktree path is unique, so folding it in gives each isolated agent
    its own slot for free. Unchanged (session_id alone) outside isolation.
    """
    return f"{session_id}\x00{override}" if override else session_id


def _is_within(path: str, root: str) -> bool:
    try:
        p = os.path.realpath(path)
        r = os.path.realpath(root)
    except OSError:
        return False
    return p == r or p.startswith(r + os.sep)


def get_cwd(session_id: str, default: str) -> str:
    """Get the persisted cwd for a session, or default if none.

    Under worktree isolation, a stored cwd is trusted only if it is actually
    inside that override — belt-and-suspenders alongside the per-worktree key,
    so a cwd from any other source can never put a command back outside the
    agent's own worktree.
    """
    override = _active_override()
    key = _key(session_id, override)
    with _lock:
        cwd = _session_cwd.get(key)
    if cwd and os.path.isdir(cwd) and (not override or _is_within(cwd, override)):
        return cwd
    return default


def update_cwd(session_id: str, cwd: str):
    """Store the working directory for a session."""
    override = _active_override()
    if override and not _is_within(cwd, override):
        return  # never let an isolated agent's cwd escape its own worktree
    key = _key(session_id, override)
    with _lock:
        _session_cwd[key] = cwd


def clear_session(session_id: str):
    """Remove persisted cwd for a session, including any isolated-agent
    entries nested under it."""
    prefix = session_id + "\x00"
    with _lock:
        _session_cwd.pop(session_id, None)
        for k in [k for k in _session_cwd if k.startswith(prefix)]:
            _session_cwd.pop(k, None)


def clear_override(session_id: str, override: str) -> None:
    """Drop the cwd entry scoped to one worktree, once that worktree is torn
    down. Without this every isolated agent run leaves a permanently orphaned
    entry for the life of the process — each worktree path is unique, so
    nothing else will ever look it up again."""
    if not override:
        return
    with _lock:
        _session_cwd.pop(_key(session_id, override), None)


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
