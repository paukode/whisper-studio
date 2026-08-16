"""
Single-file diff payloads for the UI's side-by-side viewer.

Distinct from ``server.git.diff``, which computes repo-wide *stats* for
badges and counters. This module answers a narrower question: "what
exactly changed in this one file, in a form a diff view can render?"

The server picks the rendering mode so the client never has to guess:

- ``full``   both sides shipped whole; the client aligns them and can show
             unchanged context around every edit (the VS Code look).
- ``hunks``  the file is too big to align in the browser (an LCS table is
             O(old x new) in both time and memory), so git does the diff
             in C and we ship parsed hunks with real line numbers.
- ``binary`` nothing meaningful to render line by line.
"""

import os
import re
from dataclasses import asdict, dataclass, field

from server.git.core import _run_git

# --- Constants ---

FILE_DIFF_TIMEOUT_S = 5
# Per-side ceiling for ``full`` mode. Both blobs cross the wire, so this
# also bounds the response size.
FULL_MODE_MAX_BYTES = 512_000
# The client's LCS table is (old+1) x (new+1) cells. 9M cells is ~70MB of
# packed JS numbers and a few hundred ms to fill — past that, hand the
# work to git rather than freezing the tab.
FULL_MODE_MAX_CELLS = 9_000_000
# Context lines around each hunk. Wider than git's default 3 because the
# viewer has the room and reviewers read changes in context.
HUNK_CONTEXT_LINES = 8
# Hard cap on diff lines returned in ``hunks`` mode.
MAX_HUNK_LINES = 4000

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


# --- Types ---


@dataclass
class DiffHunkLine:
    """One rendered line. ``old``/``new`` are 1-based line numbers, or
    None on the side where the line does not exist."""

    type: str  # "context" | "add" | "del"
    old: int | None
    new: int | None
    text: str


@dataclass
class DiffHunk:
    old_start: int
    new_start: int
    header: str
    lines: list[DiffHunkLine] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    mode: str  # "full" | "hunks" | "binary" | "error"
    status: str  # "modified" | "added" | "deleted"
    old: str | None = None
    new: str | None = None
    hunks: list[DiffHunk] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    truncated: bool = False
    error: str | None = None


# --- Public API ---


def build_file_diff(workspace: str, rel_path: str) -> dict:
    """Build the diff payload for one workspace-relative path.

    Always diffs against HEAD, matching the +/- stats the git panel
    already shows (``server.git.diff`` uses ``git diff HEAD --numstat``).
    Staged and unstaged edits therefore both appear.
    """
    abs_path = os.path.normpath(os.path.join(workspace, rel_path))
    if not abs_path.startswith(os.path.normpath(workspace)):
        return asdict(_error(rel_path, "Invalid path."))

    tracked = _is_tracked(workspace, rel_path)
    on_disk = os.path.isfile(abs_path)

    if not tracked and not on_disk:
        return asdict(_error(rel_path, "File not found."))

    if not tracked:
        return asdict(_untracked_diff(workspace, rel_path, abs_path))

    # One numstat call answers both "is this binary?" and "how many lines
    # changed?". They used to be two invocations of the identical command.
    is_binary, added, removed = _numstat(workspace, rel_path)
    if is_binary:
        return asdict(FileDiff(path=rel_path, mode="binary", status="modified"))

    head = _show_head(workspace, rel_path)
    if head is None:
        # Tracked but absent from HEAD — a newly added (staged) file.
        head = ""

    if not on_disk:
        return asdict(_full_or_hunks(workspace, rel_path, head, "", "deleted", added, removed))

    current = _read_text(abs_path)
    if current is None:
        return asdict(_error(rel_path, "Could not read working copy."))

    status = "added" if head == "" else "modified"
    return asdict(_full_or_hunks(workspace, rel_path, head, current, status, added, removed))


# --- Mode selection ---


def _full_or_hunks(
    workspace: str,
    rel_path: str,
    old: str,
    new: str,
    status: str,
    added: int,
    removed: int,
) -> FileDiff:
    """Ship both blobs when the client can align them, else parsed hunks.

    ``added``/``removed`` come from git's numstat so the viewer's header
    agrees with the panel's +/- badges instead of recomputing them.
    """
    if _fits_full_mode(old, new):
        return FileDiff(
            path=rel_path,
            mode="full",
            status=status,
            old=old,
            new=new,
            added=added,
            removed=removed,
        )
    return _hunks_diff(workspace, rel_path, status)


def _fits_full_mode(old: str, new: str) -> bool:
    if len(old.encode("utf-8", "replace")) > FULL_MODE_MAX_BYTES:
        return False
    if len(new.encode("utf-8", "replace")) > FULL_MODE_MAX_BYTES:
        return False
    old_lines = old.count("\n") + 1
    new_lines = new.count("\n") + 1
    return (old_lines + 1) * (new_lines + 1) <= FULL_MODE_MAX_CELLS


def _untracked_diff(workspace: str, rel_path: str, abs_path: str) -> FileDiff:
    """Untracked files have no HEAD side — every line is an addition."""
    if _has_nul_bytes(abs_path):
        return FileDiff(path=rel_path, mode="binary", status="added")

    content = _read_text(abs_path)
    if content is None:
        return _error(rel_path, "Could not read file.")

    if _fits_full_mode("", content):
        line_count = len(content.split("\n"))
        if content.endswith("\n"):
            line_count -= 1
        return FileDiff(
            path=rel_path,
            mode="full",
            status="added",
            old="",
            new=content,
            added=line_count,
            removed=0,
        )

    # Too big to align client-side: synthesize a single all-additions
    # hunk, capped. git has no diff to give us for an untracked path.
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    truncated = len(lines) > MAX_HUNK_LINES
    shown = lines[:MAX_HUNK_LINES]
    hunk = DiffHunk(old_start=0, new_start=1, header="")
    for i, text in enumerate(shown, start=1):
        hunk.lines.append(DiffHunkLine(type="add", old=None, new=i, text=text))
    return FileDiff(
        path=rel_path,
        mode="hunks",
        status="added",
        hunks=[hunk],
        added=len(lines),
        removed=0,
        truncated=truncated,
    )


# --- git-backed hunks ---


def _hunks_diff(workspace: str, rel_path: str, status: str) -> FileDiff:
    try:
        result = _run_git(
            [
                "--no-optional-locks",
                "diff",
                f"--unified={HUNK_CONTEXT_LINES}",
                "HEAD",
                "--",
                rel_path,
            ],
            cwd=workspace,
            timeout=FILE_DIFF_TIMEOUT_S,
        )
    except Exception as exc:
        return _error(rel_path, f"git diff failed: {exc}")

    if result.returncode != 0:
        return _error(rel_path, result.stderr.strip() or "git diff failed.")

    hunks, added, removed, truncated = parse_unified_diff(result.stdout)
    return FileDiff(
        path=rel_path,
        mode="hunks",
        status=status,
        hunks=hunks,
        added=added,
        removed=removed,
        truncated=truncated,
    )


def parse_unified_diff(raw: str) -> tuple[list[DiffHunk], int, int, bool]:
    """Parse a unified diff into hunks carrying both sides' line numbers.

    Returns (hunks, added, removed, truncated). Counts cover the whole
    diff even when the returned lines are capped.
    """
    hunks: list[DiffHunk] = []
    current: DiffHunk | None = None
    old_no = new_no = 0
    added = removed = 0
    emitted = 0
    truncated = False

    for line in raw.split("\n"):
        header = _HUNK_HEADER.match(line)
        if header:
            old_no = int(header.group(1))
            new_no = int(header.group(3))
            current = DiffHunk(
                old_start=old_no,
                new_start=new_no,
                header=header.group(5).strip(),
            )
            hunks.append(current)
            continue

        if current is None:
            continue  # preamble: diff/index/---/+++ lines

        if not line:
            # git writes an empty context line as a single space, so a
            # truly empty line is the trailing-newline artifact of split().
            continue

        if line.startswith("\\"):
            continue  # "\ No newline at end of file"

        marker, text = line[:1], line[1:]

        if marker == "+":
            added += 1
            if emitted < MAX_HUNK_LINES:
                current.lines.append(DiffHunkLine(type="add", old=None, new=new_no, text=text))
                emitted += 1
            else:
                truncated = True
            new_no += 1
        elif marker == "-":
            removed += 1
            if emitted < MAX_HUNK_LINES:
                current.lines.append(DiffHunkLine(type="del", old=old_no, new=None, text=text))
                emitted += 1
            else:
                truncated = True
            old_no += 1
        elif marker == " ":
            if emitted < MAX_HUNK_LINES:
                current.lines.append(
                    DiffHunkLine(type="context", old=old_no, new=new_no, text=text)
                )
                emitted += 1
            else:
                truncated = True
            old_no += 1
            new_no += 1
        # Any other marker is diff metadata between hunks — skip it.

    # Drop hunks that lost all their lines to the cap.
    hunks = [h for h in hunks if h.lines]
    return hunks, added, removed, truncated


# --- Helpers ---


def _is_tracked(workspace: str, rel_path: str) -> bool:
    try:
        result = _run_git(
            ["ls-files", "--error-unmatch", "--", rel_path],
            cwd=workspace,
            timeout=FILE_DIFF_TIMEOUT_S,
        )
        return result.returncode == 0
    except Exception:
        return False


def _numstat(workspace: str, rel_path: str) -> tuple[bool, int, int]:
    """Read one file's numstat against HEAD.

    Returns (is_binary, added, removed). git writes '-\t-\t<path>' for a
    binary file, and nothing at all for an unchanged one — both leave the
    counts at zero.
    """
    try:
        result = _run_git(
            ["--no-optional-locks", "diff", "HEAD", "--numstat", "--", rel_path],
            cwd=workspace,
            timeout=FILE_DIFF_TIMEOUT_S,
        )
    except Exception:
        return (False, 0, 0)
    if result.returncode != 0:
        return (False, 0, 0)
    for line in result.stdout.strip().split("\n"):
        if line.startswith("-\t-\t"):
            return (True, 0, 0)
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return (False, int(parts[0]), int(parts[1]))
    return (False, 0, 0)


def _has_nul_bytes(abs_path: str) -> bool:
    try:
        with open(abs_path, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return False


def _show_head(workspace: str, rel_path: str) -> str | None:
    try:
        result = _run_git(
            ["show", f"HEAD:{rel_path}"],
            cwd=workspace,
            timeout=FILE_DIFF_TIMEOUT_S,
        )
    except Exception:
        return None
    return result.stdout if result.returncode == 0 else None


def _read_text(abs_path: str) -> str | None:
    try:
        with open(abs_path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _error(rel_path: str, message: str) -> FileDiff:
    return FileDiff(path=rel_path, mode="error", status="modified", error=message)
