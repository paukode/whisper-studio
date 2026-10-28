"""
Per-session file read tracking: dedup, staleness detection, write gate.

Keyed by (session_id, the file's resolved absolute path), so a read through
a relative path and a write through the absolute one (or the reverse) are
the same file, and the same relative path in another workspace is not. No
dependencies on other server modules.
"""

import os

_read_state: dict[tuple[str, str], dict] = {}


def _key(session_id: str, abs_path: str) -> tuple[str, str]:
    return session_id, os.path.realpath(abs_path)


def record_read(
    session_id: str,
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
    _read_state[_key(session_id, abs_path)] = {
        "mtime": mtime,
        "offset": offset,
        "limit": limit,
        "full_read": full_read,
        "line_count": line_count,
    }


def record_write(session_id: str, abs_path: str):
    """Record that the app just wrote ``abs_path`` for this session (a write,
    an edit or a create, inline or approved on a card).

    The session knows the whole file it wrote, so a second edit needs no
    re-read; without this every second edit of a file failed as "modified
    since last read", and an edit of a file the session created failed as
    never read. Only the write itself counts: a formatter that changes the
    file afterwards moves the mtime, and the next write asks for a re-read.
    A later read still returns the content, never the dedup stub."""
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return
    _read_state[_key(session_id, abs_path)] = {
        "mtime": mtime,
        "offset": None,
        "limit": None,
        "full_read": True,
        "line_count": None,
        "written": True,
    }


def check_write_allowed(session_id: str, abs_path: str) -> tuple[bool, str]:
    """Check if write is allowed. Returns (allowed, reason)."""
    entry = _read_state.get(_key(session_id, abs_path))
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
    abs_path: str,
    offset: int | None,
    limit: int | None,
) -> str | None:
    """Return stub string if file unchanged since last read with same params."""
    entry = _read_state.get(_key(session_id, abs_path))
    if entry is None or entry.get("written"):
        return None
    if entry["offset"] != offset or entry["limit"] != limit:
        return None
    try:
        if os.path.getmtime(abs_path) != entry["mtime"]:
            return None
    except OSError:
        return None
    return f"(file previously read — {entry['line_count']} lines, unchanged since last read)"


def clear_session(session_id: str):
    """Remove all entries for a session."""
    keys = [k for k in _read_state if k[0] == session_id]
    for k in keys:
        del _read_state[k]
