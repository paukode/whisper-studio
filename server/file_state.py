"""
Per-session file read tracking: dedup, staleness detection, write gate.

The gate is for a whole-file write (ws_write_file): the session must know
the whole file as it is on disk, or the write replaces content the model
never saw. An edit (ws_edit_file) has no gate: it replaces text that must
match the file on disk exactly, so it cannot act on content that is stale or
unseen. A model that read the file through a shell command (cat, grep) used
to lose two calls to "must read file before writing" on each such edit.

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
    total_lines: int | None = None,
):
    """Record a file read with mtime and parameters.

    A read that returned every line counts as a read of the whole file, with
    or without a limit. A read of one part after a read of the whole file
    keeps the whole-file read while the file stays unchanged."""
    start = max(offset or 1, 1)
    full_read = (offset in (None, 1) and limit is None) or (
        total_lines is not None and start == 1 and start + line_count - 1 >= total_lines
    )
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return
    key = _key(session_id, abs_path)
    previous = _read_state.get(key)
    if previous is not None and previous["mtime"] == mtime:
        full_read = full_read or previous["full_read"]
    _read_state[key] = {
        "mtime": mtime,
        "offset": offset,
        "limit": limit,
        "full_read": full_read,
        "line_count": line_count,
    }


def record_write(session_id: str, abs_path: str, whole: bool = True):
    """Record that the app just wrote ``abs_path`` for this session (a write,
    an edit or a create, inline or approved on a card).

    After a write or a create (``whole``) the session knows the whole file,
    so a second write needs no re-read; without this every second write of a
    file failed as "modified since last read". After an edit the session
    knows the whole file only when it knew it before the edit: an edit of a
    file it never read whole leaves a whole-file write still asking for a
    read. Only the write itself counts: a formatter that changes the file
    afterwards moves the mtime, and the next write asks for a re-read. A
    later read still returns the content, never the dedup stub."""
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return
    key = _key(session_id, abs_path)
    previous = _read_state.get(key)
    _read_state[key] = {
        "mtime": mtime,
        "offset": None,
        "limit": None,
        "full_read": whole or bool(previous and previous["full_read"]),
        "line_count": None,
        "written": True,
    }


_READ_FIRST = "Read the file first (ws_read_file with no offset or limit), then repeat this call."
_READ_WHOLE = (
    "Only part of the file was read. Read it with ws_read_file with no offset or limit, "
    "then repeat this call."
)
_READ_AGAIN = (
    "The file changed on disk since it was read. Read it again with ws_read_file, "
    "then repeat this call."
)


def check_write_allowed(session_id: str, abs_path: str) -> tuple[bool, str]:
    """Whether the session may replace the whole file: it knows all of it and
    the file did not change since. Returns (allowed, reason)."""
    entry = _read_state.get(_key(session_id, abs_path))
    if entry is None:
        return False, _READ_FIRST
    try:
        current_mtime = os.path.getmtime(abs_path)
    except OSError:
        return False, "Cannot stat file."
    if current_mtime != entry["mtime"]:
        return False, _READ_AGAIN
    if not entry["full_read"]:
        return False, _READ_WHOLE
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
