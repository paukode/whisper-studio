"""
Per-session file read tracking: dedup, staleness detection, write gate.

A whole-file write needs a read of the whole file. An edit needs only the
lines it replaces: the model reads a part of a large file and edits inside
that part, which the gate used to refuse as "only partial read recorded".

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

    The lines each read returned are kept as ranges, and they add up while
    the file stays unchanged: a read of one part after a read of the whole
    file does not lose the whole-file read, and two reads of two parts cover
    both parts. An edit is then allowed where its text sits in lines that
    were read (lines_were_read)."""
    start = max(offset or 1, 1)
    end = start + line_count - 1
    full_read = (offset in (None, 1) and limit is None) or (
        total_lines is not None and start == 1 and end >= total_lines
    )
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return
    key = _key(session_id, abs_path)
    ranges = [(start, end)] if line_count > 0 else []
    previous = _read_state.get(key)
    if previous is not None and previous["mtime"] == mtime:
        full_read = full_read or previous["full_read"]
        ranges = [*previous.get("ranges", []), *ranges]
    _read_state[key] = {
        "mtime": mtime,
        "offset": offset,
        "limit": limit,
        "full_read": full_read,
        "line_count": line_count,
        "ranges": ranges,
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


_READ_FIRST = "Read the file first (ws_read_file with no offset or limit), then repeat this call."
_READ_WHOLE = (
    "Only part of the file was read. Read it with ws_read_file with no offset or limit, "
    "then repeat this call."
)
_READ_AGAIN = (
    "The file changed on disk since it was read. Read it again with ws_read_file, "
    "then repeat this call."
)


def check_read_fresh(session_id: str, abs_path: str) -> tuple[bool, str]:
    """Whether the session read the file, in whole or in part, and the file
    did not change on disk since. Returns (ok, reason)."""
    entry = _read_state.get(_key(session_id, abs_path))
    if entry is None:
        return False, _READ_FIRST
    try:
        current_mtime = os.path.getmtime(abs_path)
    except OSError:
        return False, "Cannot stat file."
    if current_mtime != entry["mtime"]:
        return False, _READ_AGAIN
    return True, ""


def check_write_allowed(session_id: str, abs_path: str) -> tuple[bool, str]:
    """Whether the session may replace the whole file: it read all of it and
    the file did not change since. Returns (allowed, reason)."""
    ok, reason = check_read_fresh(session_id, abs_path)
    if not ok:
        return False, reason
    if not _read_state[_key(session_id, abs_path)]["full_read"]:
        return False, _READ_WHOLE
    return True, ""


def lines_were_read(
    session_id: str, abs_path: str, spans: list[tuple[int, int]]
) -> tuple[bool, str]:
    """Whether every line span an edit replaces (1-based, inclusive) sits in
    lines the session read. A whole-file read covers any span. Returns
    (allowed, reason); the reason names the lines to read."""
    entry = _read_state.get(_key(session_id, abs_path))
    if entry is None:
        return False, _READ_FIRST
    if entry["full_read"]:
        return True, ""
    read = {n for start, end in entry.get("ranges", []) for n in range(start, end + 1)}
    for start, end in spans:
        if any(n not in read for n in range(start, end + 1)):
            where = f"line {start}" if start == end else f"lines {start} to {end}"
            return False, (
                f"The text to replace is at {where}, which this session did not read. "
                f"Read those lines with ws_read_file (offset {start}), then repeat the edit."
            )
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
