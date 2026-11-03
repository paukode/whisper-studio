"""
Core git utilities, git discovery, state queries, caching.

All functions are sync. Uses subprocess.run() for git commands
and direct file I/O for filesystem reads, matching Whisper's existing patterns.
"""

import os
import shutil
import subprocess
import unicodedata
from functools import lru_cache

from server.git.filesystem import (
    get_common_dir,
    is_safe_ref_name,
    read_git_head,
    resolve_ref,
)

# --- Internal helpers ---

_GIT_ROOT_CACHE: dict[str, str] = {}
_GIT_ROOT_CACHE_MAX = 50


def _run_git(
    args: list[str],
    cwd: str,
    timeout: int = 15,
    check: bool = False,
    env: dict[str, str] | None = None,
    errors: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command and return the CompletedProcess result.

    Subprocess hygiene: stdin is closed and credential-prompting is
    disabled at the env level so a missing credential fails fast instead
    of hanging the process forever. Mirrors Claude Code's `GIT_NO_PROMPT_ENV`.

    Args:
        args: Git subcommand and arguments (without 'git' prefix)
        cwd: Working directory for the command
        timeout: Timeout in seconds (default 15)
        check: If True, raise CalledProcessError on non-zero exit
        env: Extra environment variables (for example GIT_INDEX_FILE)
        errors: Decode error handler for stdout and stderr. None is strict
            UTF-8; "replace" reads output that holds non-UTF-8 file content.
    """
    extra = env or {}
    env = os.environ.copy()
    env.update(extra)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    return subprocess.run(
        [get_git_exe(), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        check=check,
        stdin=subprocess.DEVNULL,
        env=env,
        errors=errors,
    )


# --- Git executable ---


@lru_cache(maxsize=1)
def get_git_exe() -> str:
    """Memoized git executable path lookup via shutil.which()."""
    return shutil.which("git") or "git"


# --- Root discovery ---


def find_git_root(start_path: str) -> str | None:
    """Walk directory tree to locate .git, cached with a 50-entry dict cache.

    .git can be a directory (regular repo) or file (worktree/submodule).
    Returns the normalized (NFC) path to the repo root, or None if not found.
    Only a found root is cached: a folder that becomes a repository later
    (``git init``) is seen on the next call.
    """
    resolved = os.path.abspath(start_path)

    if resolved in _GIT_ROOT_CACHE:
        return _GIT_ROOT_CACHE[resolved]

    current = resolved

    while True:
        git_path = os.path.join(current, ".git")
        try:
            st = os.stat(git_path)
            # .git can be a directory (regular repo) or file (worktree/submodule)
            if st.st_mode & 0o170000 in (0o040000, 0o100000):  # dir or file
                result = unicodedata.normalize("NFC", current)
                _evict_if_full()
                _GIT_ROOT_CACHE[resolved] = result
                return result
        except OSError:
            pass

        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    return None


def _evict_if_full() -> None:
    """Evict oldest entry if cache exceeds max size."""
    if len(_GIT_ROOT_CACHE) >= _GIT_ROOT_CACHE_MAX:
        # Remove first (oldest) entry
        first_key = next(iter(_GIT_ROOT_CACHE))
        del _GIT_ROOT_CACHE[first_key]


# --- State queries ---


def get_branch(path: str) -> str:
    """Current branch name, or "HEAD" when HEAD is detached or unreadable."""
    from server.git.filesystem import resolve_git_dir

    git_dir = resolve_git_dir(path)
    if not git_dir:
        return "HEAD"
    head = read_git_head(git_dir)
    if not head:
        return "HEAD"
    return head["name"] if head["type"] == "branch" else "HEAD"


def get_head(path: str) -> str:
    """Get current HEAD SHA."""
    from server.git.filesystem import resolve_git_dir

    git_dir = resolve_git_dir(path)
    if not git_dir:
        return ""
    head = read_git_head(git_dir)
    if not head:
        return ""
    if head["type"] == "branch":
        return resolve_ref(git_dir, f"refs/heads/{head['name']}") or ""
    return head.get("sha", "")


def get_default_branch(path: str) -> str:
    """Resolve default branch name.

    Priority: origin/HEAD symref > origin/main > origin/master > 'main' fallback.
    """
    from server.git.filesystem import resolve_git_dir

    git_dir = resolve_git_dir(path)
    if not git_dir:
        return "main"

    # refs/remotes/ lives in commonDir, not the per-worktree gitDir
    common_dir = get_common_dir(git_dir) or git_dir

    # Try origin/HEAD symref first (filesystem read)
    try:
        head_path = os.path.join(common_dir, "refs", "remotes", "origin", "HEAD")
        with open(head_path) as f:
            content = f.read().strip()
        if content.startswith("ref:"):
            target = content[len("ref:") :].strip()
            prefix = "refs/remotes/origin/"
            if target.startswith(prefix):
                name = target[len(prefix) :]
                if is_safe_ref_name(name):
                    return name
    except OSError:
        pass

    # Check which common branches exist via filesystem
    for candidate in ("main", "master"):
        sha = resolve_ref(common_dir, f"refs/remotes/origin/{candidate}")
        if sha:
            return candidate

    return "main"


# --- Watcher-backed cached helpers ---
#
# These read from the GitFileWatcher cache. The watcher invalidates on
# .git/HEAD, .git/config, or refs/heads/<branch> change, so values stay
# correct without any timer or manual invalidation. Hot paths (panel,
# prompt building, status fetches) should use these instead of the
# uncached forms above.


def get_cached_branch(path: str) -> str:
    from server.git.watcher import git_watcher

    return git_watcher.get(f"branch:{path}", lambda: get_branch(path))


def get_cached_head(path: str) -> str:
    from server.git.watcher import git_watcher

    return git_watcher.get(f"head:{path}", lambda: get_head(path))


# NUL-separated so paths come back unquoted (a space or a non-ASCII byte
# makes plain --porcelain wrap the path in quotes) and renames need no
# "old -> new" splitting.
STATUS_PORCELAIN_ARGS = ["--no-optional-locks", "status", "--porcelain=v1", "-z"]


def parse_porcelain_status(stdout: str) -> list[tuple[str, str]]:
    """Split `git status --porcelain=v1 -z` output into (code, path) pairs.

    Each entry is "XY PATH". A rename or copy is followed by one more field,
    the source path, which is skipped: the pair reports the new path. The
    output is never stripped, because the first entry can start with a space
    (an unstaged change has a blank index column) and stripping it would
    shift the code into the staged column.
    """
    entries: list[tuple[str, str]] = []
    fields = stdout.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        code = entry[:2]
        entries.append((code, entry[3:]))
        if "R" in code or "C" in code:
            i += 1
    return entries


def get_file_status(path: str) -> dict:
    """Get tracked and untracked file lists.

    Returns: {"tracked": [...], "untracked": [...]}. Raises RuntimeError with
    git's message when `git status` fails (a corrupt index, a "dubious
    ownership" refusal), so a broken repository never reads as clean.
    """
    result = _run_git(STATUS_PORCELAIN_ARGS, cwd=path)
    if result.returncode != 0:
        raise RuntimeError(f"git status failed: {result.stderr.strip()}")
    tracked = []
    untracked = []
    for status, filename in parse_porcelain_status(result.stdout):
        if status == "??":
            untracked.append(filename)
        else:
            tracked.append(filename)
    return {"tracked": tracked, "untracked": untracked}
