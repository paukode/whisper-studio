"""
Per-session file read tracking — dedup, staleness detection, write gate.

Keyed by (session_id, relative_path). No dependencies on other server modules.
"""

import os

_read_state: dict[tuple[str, str], dict] = {}


def record_read(
    session_id: str,
    rel_path: str,
    abs_path: str,
    offset: int | None,
    limit: int | None,
    line_count: int,
):
    """Record a file read with mtime and parameters."""
    full_read = offset in (None, 1) and limit is None
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return
    _read_state[(session_id, rel_path)] = {
        "mtime": mtime,
        "offset": offset,
        "limit": limit,
        "full_read": full_read,
        "line_count": line_count,
    }


def check_write_allowed(session_id: str, rel_path: str, abs_path: str) -> tuple[bool, str]:
    """Check if write is allowed. Returns (allowed, reason)."""
    key = (session_id, rel_path)
    entry = _read_state.get(key)
    if entry is None:
        return False, "Must read file before writing."
    if not entry["full_read"]:
        return False, "Only partial read recorded. Read full file first."
    try:
        current_mtime = os.path.getmtime(abs_path)
    except OSError:
        return False, "Cannot stat file."
    if current_mtime != entry["mtime"]:
        return False, "File modified since last read. Re-read before writing."
    return True, ""


def check_dedup(
    session_id: str,
    rel_path: str,
    abs_path: str,
    offset: int | None,
    limit: int | None,
) -> str | None:
    """Return stub string if file unchanged since last read with same params."""
    key = (session_id, rel_path)
    entry = _read_state.get(key)
    if entry is None:
        return None
    if entry["offset"] != offset or entry["limit"] != limit:
        return None
    try:
        if os.path.getmtime(abs_path) != entry["mtime"]:
            return None
    except OSError:
        return None
    return f"(file previously read — {entry['line_count']} lines, unchanged since last read)"


def update_after_write(session_id: str, rel_path: str, abs_path: str):
    """Update mtime after a successful write."""
    key = (session_id, rel_path)
    entry = _read_state.get(key)
    if entry is None:
        return
    try:
        entry["mtime"] = os.path.getmtime(abs_path)
    except OSError:
        pass


def clear_session(session_id: str):
    """Remove all entries for a session."""
    keys = [k for k in _read_state if k[0] == session_id]
    for k in keys:
        del _read_state[k]
