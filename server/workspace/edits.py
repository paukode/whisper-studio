"""The edits the assistant made, kept per tool_use_id for the diff dialog.

Every write, edit or create of a workspace file runs through
server/approval/executors.py _do_write, on every approval path. It records
the file before and after the write here, keyed by the provider's tool_use
id, so the activity row of that call can open a diff later, long after the
turn ended. The row's own numbers (added, removed) come from the same
``diff_stats`` the executor computes once; the dialog reads the texts back
through GET /api/workspace/edits/{tool_use_id} (server/workspace/routes).

Records live under ``<data root>/edits/<tool_use_id>.json`` and hold
{path, original, modified, added, removed, approximate, created, at,
stored}. The texts are not kept for a text over EDIT_TEXT_CAP bytes, or for
a dotenv or credential file (a key, a .env, a credentials.json): the record
is still written, with both texts null and ``stored`` false, so the dialog
can say the diff was not kept. A prune removes records older than
PRUNE_AFTER_DAYS and leftover temporary files, then the oldest records until
all of them fit in EDITS_TOTAL_CAP. It runs the first time an edit is
recorded in a process, again once a day after that, and whenever the records
outgrow the cap. Deleting a session removes the records of the calls its
transcript holds (forget_session_edits); a branch of that session then
shows its copies of those rows as not kept, as it does its attachments.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import UTC, datetime

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

# A text larger than this, in UTF-8 bytes, is not stored (the dialog says
# so instead).
EDIT_TEXT_CAP = 2_000_000
PRUNE_AFTER_DAYS = 30
# How often a long-running process prunes again.
PRUNE_EVERY_SECONDS = 86_400
# The most the records may hold together, in bytes; the oldest go first.
EDITS_TOTAL_CAP = 200_000_000
# A temporary file this old is left over from a write that never finished.
STALE_TMP_SECONDS = 3_600

# The bounds of the exact diff. SequenceMatcher without autojunk compares
# every pair of equal lines on each matching pass, so a file with many
# repeated lines (a lockfile, generated JSON) costs about the cube of its
# size: a replace-all on a 4,000-line lockfile took 29 s. Over either bound
# the counts come from the cheap multiset fallback and are marked
# approximate. EXACT_PAIR_BUDGET caps the line pairs one pass may compare
# (about 0.1 s of matching); EXACT_DEADLINE_SECONDS caps the passes together.
# Source files, whose lines are mostly unique, finish far inside both.
EXACT_PAIR_BUDGET = 500_000
EXACT_DEADLINE_SECONDS = 0.25

# A tool_use id is a file name here, so only a plain token is accepted.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A dotenv file: .env, .env.local, .env.production and the like, in any
# case (APFS is case-insensitive, so .ENV is .env).
_DOTENV = re.compile(r"(?:^|/)\.env(?:\.[^/]*)?$", re.IGNORECASE)
# Secret files the command validator's names do not cover, by file name in
# any case: a bare SSH private key (id_rsa, never id_rsa.pub), a direnv
# file, any *.env file, Terraform variables and state, and secrets files.
_SECRET_NAME = re.compile(
    r"(?:^|/)(?:id_(?:rsa|ed25519|ecdsa|dsa)|\.envrc|[^/]+\.env|[^/]+\.tfvars(?:\.json)?"
    r"|[^/]+\.tfstate(?:\.backup)?|\.?secrets?(?:\.(?:ya?ml|json|toml|env))?)$",
    re.IGNORECASE,
)

# The prune's bookkeeping, shared by the worker threads that record edits:
# when it last ran (None: not yet in this process) and the bytes the records
# held then, plus every record written since.
_lock = threading.Lock()
_last_prune: float | None = None
_total_bytes = 0


def edits_dir() -> str:
    return os.path.join(data_root(), "edits")


def _record_path(tool_use_id: str) -> str | None:
    if not _SAFE_ID.match(tool_use_id or ""):
        return None
    return os.path.join(edits_dir(), f"{tool_use_id}.json")


def split_lines(text: str) -> list[str]:
    """``text`` split on newlines alone, each line keeping its end, the way
    git counts lines (str.splitlines also splits on a form feed, U+2028 and
    other separators)."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


class _OutOfTime(Exception):
    """The exact diff ran past EXACT_DEADLINE_SECONDS."""


class _BoundedMatcher(difflib.SequenceMatcher):
    """Exact line matching (no autojunk) that gives up at a deadline. Each
    matching pass starts with a find_longest_match call, so the check runs
    once per pass."""

    def __init__(self, a: list[str], b: list[str], deadline: float):
        self._deadline = deadline
        super().__init__(None, a, b, autojunk=False)

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
        if time.monotonic() > self._deadline:
            raise _OutOfTime
        return super().find_longest_match(alo, ahi, blo, bhi)


def _changed_lines(old: list[str], new: list[str]) -> tuple[int, int, bool]:
    """(added, removed, approximate) from ``old`` to ``new``."""
    # The common head and tail are cheap to drop and most edits touch one
    # place, so the matcher usually sees a few lines only.
    limit = min(len(old), len(new))
    head = 0
    while head < limit and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < limit - head and old[-1 - tail] == new[-1 - tail]:
        tail += 1
    old_mid = old[head : len(old) - tail]
    new_mid = new[head : len(new) - tail]
    if not old_mid or not new_mid:
        return len(new_mid), len(old_mid), False
    old_count, new_count = Counter(old_mid), Counter(new_mid)
    pairs = sum(n * new_count.get(line, 0) for line, n in old_count.items())
    if pairs <= EXACT_PAIR_BUDGET:
        matcher = _BoundedMatcher(old_mid, new_mid, time.monotonic() + EXACT_DEADLINE_SECONDS)
        try:
            matched = sum(block.size for block in matcher.get_matching_blocks())
        except _OutOfTime:
            pass
        else:
            return len(new_mid) - matched, len(old_mid) - matched, False
    # The lines each side holds more often than the other: exact for a
    # replace-all or a rewrite, and an undercount when lines moved.
    added = sum((new_count - old_count).values())
    removed = sum((old_count - new_count).values())
    return added, removed, True


def diff_stats(old: str | None, new: str) -> dict:
    """Lines added and removed from ``old`` to ``new``, with the line count
    and byte size of ``new``. ``approximate`` is true when the exact diff was
    over its bounds and the counts come from the fallback. ``old`` is None
    when the file before the write could not be read: added and removed are
    then unknown (None)."""
    new_lines = split_lines(new)
    added = removed = None
    approximate = False
    if old is not None:
        added, removed, approximate = _changed_lines(split_lines(old), new_lines)
    return {
        "added": added,
        "removed": removed,
        "lines": len(new_lines),
        "bytes": len(new.encode("utf-8")),
        "stored": False,
        "approximate": approximate,
    }


def _fits(text: str) -> bool:
    """Whether ``text`` is small enough to keep: EDIT_TEXT_CAP is in bytes,
    and a character can be up to four of them."""
    if len(text) * 4 <= EDIT_TEXT_CAP:
        return True
    return len(text.encode("utf-8")) <= EDIT_TEXT_CAP


def holds_secrets(path: str) -> bool:
    """Whether ``path`` (workspace-relative) names a dotenv or credential
    file, whose texts are not kept in plain JSON: a dotenv file, the names
    in _SECRET_NAME, and the command validator's credential names and
    extensions (server/security), all in any case."""
    from server.security.command_validator import check_sensitive_paths

    rel = path.replace(os.sep, "/")
    if _DOTENV.search(rel) or _SECRET_NAME.search(rel):
        return True
    return check_sensitive_paths("/" + rel.lstrip("/").lower()) is not None


def record_edit(
    tool_use_id: str,
    path: str,
    original: str,
    modified: str,
    stats: dict,
    *,
    created: bool = False,
) -> bool:
    """Write the record for ``tool_use_id`` and set ``stats["stored"]``.
    ``created`` says the write made the file, which an empty ``original``
    alone cannot tell from an existing empty file.

    Returns False, with nothing written, for an id that is not a plain token
    or when the folder cannot be written; the write the record describes has
    already happened, so this never raises."""
    target = _record_path(tool_use_id)
    if target is None:
        stats["stored"] = False
        return False
    keep_texts = not holds_secrets(path) and _fits(original) and _fits(modified)
    record = {
        "path": path,
        "original": original if keep_texts else None,
        "modified": modified if keep_texts else None,
        "added": stats.get("added"),
        "removed": stats.get("removed"),
        "approximate": bool(stats.get("approximate")),
        "created": created,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "stored": keep_texts,
    }
    try:
        os.makedirs(edits_dir(), exist_ok=True)
        tmp = f"{target}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, target)
        size = os.path.getsize(target)
    except OSError as e:
        log.warning("edits: could not record %s: %s", tool_use_id, e)
        stats["stored"] = False
        return False
    stats["stored"] = keep_texts
    _after_record(size)
    return True


def _after_record(size: int) -> None:
    """Count a new record and prune when one is due: the first record in this
    process, a day after the last prune, or records over EDITS_TOTAL_CAP."""
    global _last_prune, _total_bytes
    now = time.time()
    with _lock:
        _total_bytes += size
        due = (
            _last_prune is None
            or now - _last_prune >= PRUNE_EVERY_SECONDS
            or _total_bytes > EDITS_TOTAL_CAP
        )
        if not due:
            return
        _last_prune = now
        try:
            prune_old(now)
        except OSError as e:
            log.warning("edits: prune failed: %s", e)


def read_edit(tool_use_id: str) -> dict | None:
    """The record for ``tool_use_id``, or None when there is none."""
    target = _record_path(tool_use_id)
    if target is None or not os.path.isfile(target):
        return None
    try:
        with open(target, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.warning("edits: could not read %s: %s", tool_use_id, e)
        return None


def call_ids(history) -> set[str]:
    """The provider ids of the tool calls a saved transcript holds (the
    chat_history rows of server/infrastructure/sessions.py): each row's
    toolUse entries, whose toolId is the id when the call had one and the
    tool name otherwise, and its raw traces."""
    ids: set[str] = set()
    for row in history if isinstance(history, list) else []:
        if not isinstance(row, dict):
            continue
        for call in row.get("toolUse") or []:
            if isinstance(call, dict):
                tool_id = call.get("toolId")
                if isinstance(tool_id, str) and tool_id and tool_id != call.get("toolName"):
                    ids.add(tool_id)
        for trace in row.get("traces") or []:
            if isinstance(trace, dict) and isinstance(trace.get("id"), str) and trace["id"]:
                ids.add(trace["id"])
    return ids


def forget_session_edits(history) -> int:
    """Remove the records of the calls in ``history``, a deleted session's
    transcript; returns how many went. Best-effort: a record that cannot be
    removed is left to the age prune."""
    removed = 0
    for tool_use_id in call_ids(history):
        target = _record_path(tool_use_id)
        if target is None:
            continue
        try:
            os.remove(target)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("edits: could not remove %s: %s", tool_use_id, e)
    return removed


def prune_old(now: float | None = None) -> int:
    """Remove records older than PRUNE_AFTER_DAYS and stale temporary files,
    then the oldest records until the rest fit in EDITS_TOTAL_CAP. Records
    the size that is left for the next check; returns how many files went."""
    global _total_bytes
    now = time.time() if now is None else now
    cutoff = now - PRUNE_AFTER_DAYS * 86400
    folder = edits_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        _total_bytes = 0
        return 0
    removed = 0
    kept: list[tuple[float, int, str]] = []
    for name in names:
        full = os.path.join(folder, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if name.endswith(".tmp"):
            stale = st.st_mtime < now - STALE_TMP_SECONDS
        elif name.endswith(".json"):
            stale = st.st_mtime < cutoff
        else:
            continue
        if stale:
            try:
                os.remove(full)
                removed += 1
            except OSError:
                pass
        elif name.endswith(".json"):
            kept.append((st.st_mtime, st.st_size, full))
    total = sum(size for _mtime, size, _full in kept)
    kept.sort()
    for _mtime, size, full in kept:
        if total <= EDITS_TOTAL_CAP:
            break
        try:
            os.remove(full)
        except OSError:
            continue
        total -= size
        removed += 1
    _total_bytes = total
    if removed:
        log.info("edits: pruned %d file(s)", removed)
    return removed
