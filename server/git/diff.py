"""
Git diff system — fetching and parsing with size safeguards.

Provides diff stats, per-file stats, and structured hunks with intelligent
size limits. Detects transient git states (merge/rebase/cherry-pick/revert)
and skips diff during those operations.
"""

import os
import re
from dataclasses import dataclass, field

from server.git.core import (
    _run_git,
    find_git_root,
    get_default_branch,
    get_remote_url,
)
from server.git.filesystem import resolve_git_dir

# --- Constants (matching reference implementation) ---

GIT_TIMEOUT_S = 5
MAX_FILES = 50  # Per-file detail limit
MAX_DIFF_SIZE_BYTES = 1_000_000  # 1MB — skip files larger than this
MAX_LINES_PER_FILE = 400  # GitHub's auto-load limit
MAX_FILES_FOR_DETAILS = 500  # Skip per-file details if more files than this
MAX_UNTRACKED_FILES = 20_000
SINGLE_FILE_DIFF_TIMEOUT_S = 3


# --- Types ---


@dataclass
class DiffFileStats:
    added: int
    removed: int
    is_binary: bool
    is_untracked: bool = False


@dataclass
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[str] = field(default_factory=list)


@dataclass
class GitDiffResult:
    files_count: int
    lines_added: int
    lines_removed: int
    per_file_stats: dict[str, DiffFileStats]
    hunks: dict[str, list[DiffHunk]]


@dataclass
class ToolUseDiff:
    filename: str
    status: str  # "modified" or "added"
    additions: int
    deletions: int
    changes: int
    patch: str
    repository: str | None = None


# --- Transient state detection ---


def is_transient_git_state(git_dir: str) -> bool:
    """Detect if repo is in a transient git state (merge/rebase/cherry-pick/revert).

    During these operations the working tree contains incoming changes,
    not user-intentional edits. Diff results would be misleading.
    """
    transient_indicators = [
        "MERGE_HEAD",
        "REBASE_HEAD",
        os.path.join("rebase-merge", ""),  # directory existence
        os.path.join("rebase-apply", ""),  # directory existence
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
    ]
    for indicator in transient_indicators:
        path = os.path.join(git_dir, indicator)
        if indicator.endswith(os.sep):
            if os.path.isdir(path):
                return True
        else:
            if os.path.isfile(path):
                return True
    return False


# --- Diff fetching ---


def fetch_git_diff(path: str) -> GitDiffResult | None:
    """Compare working tree to HEAD. Returns stats + per-file stats + hunks.

    Returns None if not in a git repo, during transient git states,
    or if git commands fail. Hunks are NOT included — use fetch_git_diff_hunks()
    for on-demand hunk loading to avoid expensive calls during polling.
    """
    git_root = find_git_root(path)
    if not git_root:
        return None

    git_dir = resolve_git_dir(path)
    if not git_dir:
        return None

    # Skip during transient states
    if is_transient_git_state(git_dir):
        return None

    # Quick probe: shortstat is O(1) memory regardless of diff size
    shortstat = parse_shortstat(path)
    if shortstat is None:
        return None

    files_count, lines_added, lines_removed = shortstat

    # If too many files, return accurate totals but skip per-file details
    if files_count > MAX_FILES_FOR_DETAILS:
        return GitDiffResult(
            files_count=files_count,
            lines_added=lines_added,
            lines_removed=lines_removed,
            per_file_stats={},
            hunks={},
        )

    # Get per-file stats via numstat
    numstat_result = parse_git_numstat(path)
    if numstat_result is None:
        return GitDiffResult(
            files_count=files_count,
            lines_added=lines_added,
            lines_removed=lines_removed,
            per_file_stats={},
            hunks={},
        )

    per_file_stats = numstat_result

    # Add untracked files (up to remaining slots)
    remaining_slots = MAX_FILES - len(per_file_stats)
    if remaining_slots > 0:
        untracked = _fetch_untracked_files(git_root, remaining_slots)
        if untracked:
            per_file_stats.update(untracked)

    return GitDiffResult(
        files_count=files_count + sum(1 for s in per_file_stats.values() if s.is_untracked),
        lines_added=lines_added,
        lines_removed=lines_removed,
        per_file_stats=per_file_stats,
        hunks={},  # hunks loaded on-demand
    )


def fetch_git_diff_hunks(path: str) -> dict[str, list[DiffHunk]]:
    """Fetch git diff hunks on-demand.

    Separated from fetch_git_diff() to avoid expensive calls during polling.
    """
    git_root = find_git_root(path)
    if not git_root:
        return {}

    git_dir = resolve_git_dir(path)
    if not git_dir:
        return {}

    if is_transient_git_state(git_dir):
        return {}

    try:
        result = _run_git(
            ["--no-optional-locks", "diff", "HEAD"],
            cwd=git_root,
            timeout=GIT_TIMEOUT_S,
        )
        if result.returncode != 0:
            return {}
        return parse_git_diff(result.stdout)
    except Exception:
        return {}


def fetch_single_file_git_diff(path: str, file_path: str) -> ToolUseDiff | None:
    """Fetch structured diff for a single file against merge base (PR-like diff).

    Falls back to diffing against HEAD if merge base cannot be determined.
    For untracked files, generates synthetic diff showing all additions.
    """
    git_root = find_git_root(os.path.dirname(os.path.abspath(file_path)))
    if not git_root:
        return None

    abs_file = os.path.abspath(file_path)
    rel_file = os.path.relpath(abs_file, git_root)

    # Check if file is tracked
    try:
        track_result = _run_git(
            ["ls-files", "--error-unmatch", rel_file],
            cwd=git_root,
            timeout=SINGLE_FILE_DIFF_TIMEOUT_S,
        )
        is_tracked = track_result.returncode == 0
    except Exception:
        is_tracked = False

    if not is_tracked:
        # Untracked file — generate synthetic diff
        return _generate_synthetic_diff(abs_file, rel_file, git_root)

    # Get diff reference (merge base or HEAD)
    diff_ref = _get_diff_ref(git_root)

    try:
        result = _run_git(
            ["diff", diff_ref, "--", rel_file],
            cwd=git_root,
            timeout=SINGLE_FILE_DIFF_TIMEOUT_S,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return _parse_raw_diff_to_tool_use(rel_file, result.stdout, git_root)
    except Exception:
        return None


# --- Parsing ---


def parse_git_numstat(path: str) -> dict[str, DiffFileStats] | None:
    """Parse 'git diff --numstat' output into per-file stats.

    Format: <added>\\t<removed>\\t<filename>
    Binary files show '-' for counts.
    Only stores first MAX_FILES entries.
    """
    git_root = find_git_root(path)
    if not git_root:
        return None

    try:
        result = _run_git(
            ["--no-optional-locks", "diff", "HEAD", "--numstat"],
            cwd=git_root,
            timeout=GIT_TIMEOUT_S,
        )
        if result.returncode != 0:
            return None
    except Exception:
        return None

    per_file: dict[str, DiffFileStats] = {}
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue

        added_str, removed_str = parts[0], parts[1]
        # Filenames with tabs: rejoin remaining parts
        filename = "\t".join(parts[2:])

        if added_str == "-" and removed_str == "-":
            # Binary file
            if len(per_file) < MAX_FILES:
                per_file[filename] = DiffFileStats(added=0, removed=0, is_binary=True)
        else:
            try:
                added = int(added_str)
                removed = int(removed_str)
            except ValueError:
                continue
            if len(per_file) < MAX_FILES:
                per_file[filename] = DiffFileStats(added=added, removed=removed, is_binary=False)

    return per_file


def parse_git_diff(output: str) -> dict[str, list[DiffHunk]]:
    """Parse unified diff output into per-file hunks.

    Applies limits:
    - MAX_FILES: stop after this many files
    - MAX_DIFF_SIZE_BYTES: skip files entirely if larger
    - MAX_LINES_PER_FILE: limit lines per file
    """
    hunks: dict[str, list[DiffHunk]] = {}

    # Split by "diff --git" pattern
    sections = re.split(r"^diff --git ", output, flags=re.MULTILINE)

    file_count = 0
    for section in sections:
        if not section.strip():
            continue

        file_count += 1
        if file_count > MAX_FILES:
            break

        # Skip oversized sections
        if len(section.encode("utf-8", errors="replace")) > MAX_DIFF_SIZE_BYTES:
            continue

        # Extract filename from "a/path b/path" header
        header_line = section.split("\n", 1)[0]
        filename = _extract_filename_from_diff_header(header_line)
        if not filename:
            continue

        file_hunks = []
        current_hunk = None
        line_count = 0

        for line in section.split("\n"):
            # Hunk header
            hunk_match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", line)
            if hunk_match:
                if current_hunk:
                    file_hunks.append(current_hunk)
                current_hunk = DiffHunk(
                    old_start=int(hunk_match.group(1)),
                    old_count=int(hunk_match.group(2) or "1"),
                    new_start=int(hunk_match.group(3)),
                    new_count=int(hunk_match.group(4) or "1"),
                    header=line,
                )
                continue

            if current_hunk is None:
                # Skip metadata lines (index, ---, +++, new file, etc.)
                continue

            # Only include diff content lines (+, -, space)
            if line and line[0] in ("+", "-", " "):
                line_count += 1
                if line_count <= MAX_LINES_PER_FILE:
                    current_hunk.lines.append(line)

        if current_hunk:
            file_hunks.append(current_hunk)

        if file_hunks:
            hunks[filename] = file_hunks

    return hunks


def parse_shortstat(path: str) -> tuple[int, int, int] | None:
    """Parse 'git diff --shortstat' for quick totals.

    Returns (files_changed, insertions, deletions) or None on failure.
    O(1) memory — git computes totals without loading all content.
    """
    git_root = find_git_root(path)
    if not git_root:
        return None

    try:
        result = _run_git(
            ["--no-optional-locks", "diff", "HEAD", "--shortstat"],
            cwd=git_root,
            timeout=GIT_TIMEOUT_S,
        )
        if result.returncode != 0:
            return None
    except Exception:
        return None

    output = result.stdout.strip()
    if not output:
        return (0, 0, 0)

    files = 0
    insertions = 0
    deletions = 0

    files_match = re.search(r"(\d+) files? changed", output)
    if files_match:
        files = int(files_match.group(1))

    ins_match = re.search(r"(\d+) insertions?\(\+\)", output)
    if ins_match:
        insertions = int(ins_match.group(1))

    del_match = re.search(r"(\d+) deletions?\(-\)", output)
    if del_match:
        deletions = int(del_match.group(1))

    return (files, insertions, deletions)


# --- Private helpers ---


def _fetch_untracked_files(git_root: str, max_files: int) -> dict[str, DiffFileStats]:
    """Fetch untracked file stats (excluding ignored files)."""
    try:
        result = _run_git(
            ["ls-files", "--others", "--exclude-standard"],
            cwd=git_root,
            timeout=GIT_TIMEOUT_S,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
    except Exception:
        return {}

    untracked: dict[str, DiffFileStats] = {}
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        if len(untracked) >= max_files:
            break
        untracked[line] = DiffFileStats(added=0, removed=0, is_binary=False, is_untracked=True)
    return untracked


def _get_diff_ref(git_root: str) -> str:
    """Get the best reference for diffing.

    Priority:
    1. CLAUDE_CODE_BASE_REF environment variable
    2. Merge base with default branch
    3. HEAD (fallback)
    """
    base_ref = os.environ.get("CLAUDE_CODE_BASE_REF")
    if base_ref:
        return base_ref.strip()

    default_branch = get_default_branch(git_root)
    try:
        result = _run_git(
            ["merge-base", "HEAD", f"origin/{default_branch}"],
            cwd=git_root,
            timeout=SINGLE_FILE_DIFF_TIMEOUT_S,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    return "HEAD"


def _generate_synthetic_diff(abs_file: str, rel_file: str, git_root: str) -> ToolUseDiff | None:
    """Generate synthetic diff for untracked files (all lines as additions)."""
    try:
        file_size = os.path.getsize(abs_file)
        if file_size > MAX_DIFF_SIZE_BYTES:
            return None

        with open(abs_file, errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    lines = content.split("\n")
    # Remove trailing empty line if file ends with newline
    if lines and lines[-1] == "":
        lines = lines[:-1]

    line_count = len(lines)
    patch_lines = [f"@@ -0,0 +1,{line_count} @@"]
    patch_lines.extend(f"+{line}" for line in lines)
    patch = "\n".join(patch_lines)

    # Detect repository for metadata
    repository = _get_github_repo(git_root)

    return ToolUseDiff(
        filename=rel_file,
        status="added",
        additions=line_count,
        deletions=0,
        changes=line_count,
        patch=patch,
        repository=repository,
    )


def _parse_raw_diff_to_tool_use(filename: str, raw_diff: str, git_root: str) -> ToolUseDiff:
    """Parse raw unified diff into ToolUseDiff format."""
    additions = 0
    deletions = 0
    patch_lines = []
    in_patch = False

    for line in raw_diff.split("\n"):
        if line.startswith("@@"):
            in_patch = True
        if in_patch:
            patch_lines.append(line)
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1

    repository = _get_github_repo(git_root)

    return ToolUseDiff(
        filename=filename,
        status="modified",
        additions=additions,
        deletions=deletions,
        changes=additions + deletions,
        patch="\n".join(patch_lines),
        repository=repository,
    )


def _get_github_repo(git_root: str) -> str | None:
    """Get GitHub 'owner/repo' string if this is a github.com repo."""
    from server.git.core import normalize_git_remote_url

    remote_url = get_remote_url(git_root)
    if not remote_url:
        return None
    normalized = normalize_git_remote_url(remote_url)
    if not normalized:
        return None
    # Only return for github.com repos
    if normalized.startswith("github.com/"):
        return normalized[len("github.com/") :]
    return None


def _extract_filename_from_diff_header(header: str) -> str | None:
    """Extract filename from diff --git header line 'a/path b/path'."""
    # Format: "a/path b/path"
    parts = header.split(" b/", 1)
    if len(parts) == 2:
        return parts[1].strip()
    # Fallback: try to extract from a/ prefix
    parts = header.split(" a/", 1)
    if len(parts) == 2:
        remainder = parts[1]
        space_idx = remainder.find(" b/")
        if space_idx != -1:
            return remainder[space_idx + 3 :].strip()
    return None
