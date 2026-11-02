"""Undo for one write the assistant made, kept per tool_use_id.

server/approval/executors.py _do_write runs every approved write, create or
edit of a workspace file, on every approval path (the card's Yes, an inline
run in Bypass, a spoken yes, an unattended agent). When the call has a
tool_use id, it keeps here the exact bytes of the file from immediately
before that write (None when the write created the file) and a hash of the
file as the write left it (after ruff, which can fix or reformat it).

Undo (POST /api/workspace/undo with the tool_use_id) puts back the bytes of
that one write, or removes the file the write created. It refuses when the
file is not as that write left it: a later write, an edit by the user, or a
move. So two writes to one file undo in order, the later one first, and an
undo never overwrites a change it did not make.

The copies live in memory only: a restart forgets them, a workspace change
or disconnect clears them, and the oldest go first when they outgrow
UNDO_TOTAL_CAP. A successful undo uses its copy up.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

# The most the copies may hold together, in bytes; the oldest go first.
UNDO_TOTAL_CAP = 64_000_000


@dataclass(frozen=True)
class WriteBackup:
    full: str
    path: str
    session_id: str | None
    before: bytes | None
    after_sha: str


class UndoRefused(Exception):
    """The undo did not change the file; ``status`` is the HTTP status."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


_LOCK = threading.Lock()
_BACKUPS: OrderedDict[str, WriteBackup] = OrderedDict()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(full: str) -> bytes | None:
    try:
        with open(full, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _size(backup: WriteBackup) -> int:
    return len(backup.before or b"")


def record_write(
    tool_use_id: str, full: str, path: str, session_id: str | None, before: bytes | None
) -> None:
    """Keep the file before the write of ``tool_use_id``, with the hash of
    the file as it is now (call this after the write and any ruff fix).
    Raises OSError when the file cannot be read back: then no undo is kept."""
    after = _read(full)
    if after is None:
        raise FileNotFoundError(full)
    backup = WriteBackup(full, path, session_id, before, _sha(after))
    with _LOCK:
        _BACKUPS.pop(tool_use_id, None)
        _BACKUPS[tool_use_id] = backup
        total = sum(_size(b) for b in _BACKUPS.values())
        while total > UNDO_TOTAL_CAP and len(_BACKUPS) > 1:
            _, dropped = _BACKUPS.popitem(last=False)
            total -= _size(dropped)
        if total > UNDO_TOTAL_CAP:
            _BACKUPS.clear()


def has_undo(tool_use_id: str) -> bool:
    with _LOCK:
        return tool_use_id in _BACKUPS


def get_backup(tool_use_id: str) -> WriteBackup | None:
    with _LOCK:
        return _BACKUPS.get(tool_use_id)


def undo_write(tool_use_id: str, write_bytes) -> WriteBackup:
    """Put back the file from before the write of ``tool_use_id``.

    ``write_bytes(full, data)`` writes the file atomically. Raises
    UndoRefused, with the reason for the user, when nothing was changed."""
    with _LOCK:
        backup = _BACKUPS.get(tool_use_id)
        if backup is None:
            raise UndoRefused(
                "No undo copy is kept for this change. The app keeps it only until a restart "
                "or a change of workspace.",
                status=404,
            )
        current = _read(backup.full)
        if current is None:
            raise UndoRefused(
                f"{backup.path} was moved or deleted after this change. Undo did not change it."
            )
        if _sha(current) != backup.after_sha:
            raise UndoRefused(
                f"{backup.path} changed after this change was written. Undo did not change it, "
                "so that the later changes stay. Undo the later changes first, if they came "
                "from the assistant."
            )
        if backup.before is None:
            os.remove(backup.full)
        else:
            write_bytes(backup.full, backup.before)
        del _BACKUPS[tool_use_id]
        return backup


def clear_write_backups() -> None:
    with _LOCK:
        _BACKUPS.clear()
