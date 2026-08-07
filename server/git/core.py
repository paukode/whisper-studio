"""
Core git utilities — git discovery, state queries, caching.

All functions are sync. Uses subprocess.run() for git commands
and direct file I/O for filesystem reads, matching Whisper's existing patterns.
"""

import os
import re
import shutil
import subprocess
import unicodedata
from functools import lru_cache

from server.git.config_parser import parse_git_config_value
from server.git.filesystem import (
    get_common_dir,
    is_safe_ref_name,
    read_git_head,
    resolve_ref,
)

# --- Internal helpers ---

_GIT_ROOT_CACHE: dict[str, str | None] = {}
_GIT_ROOT_CACHE_MAX = 50


def _run_git(
    args: list[str],
    cwd: str,
    timeout: int = 15,
    check: bool = False,
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
    """
    env = os.environ.copy()
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
    """
    resolved = os.path.abspath(start_path)

    if resolved in _GIT_ROOT_CACHE:
        return _GIT_ROOT_CACHE[resolved]

    current = resolved
    root = os.path.splitdrive(current)[0] + os.sep if os.name == "nt" else "/"

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

    # Check root directory as well
    git_path = os.path.join(root, ".git")
    try:
        st = os.stat(git_path)
        if st.st_mode & 0o170000 in (0o040000, 0o100000):
            result = unicodedata.normalize("NFC", root)
            _evict_if_full()
            _GIT_ROOT_CACHE[resolved] = result
            return result
    except OSError:
        pass

    _evict_if_full()
    _GIT_ROOT_CACHE[resolved] = None
    return None


def _evict_if_full() -> None:
    """Evict oldest entry if cache exceeds max size."""
    if len(_GIT_ROOT_CACHE) >= _GIT_ROOT_CACHE_MAX:
        # Remove first (oldest) entry
        first_key = next(iter(_GIT_ROOT_CACHE))
        del _GIT_ROOT_CACHE[first_key]


# --- State queries ---


def get_branch(path: str) -> str:
    """Get current branch name. Falls back to HEAD SHA if detached, or 'HEAD' on error."""
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
    Uses filesystem reads first, falls back to subprocess for remote show.
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


def get_remote_url(path: str) -> str | None:
    """Get remote origin URL. Tries filesystem first, falls back to git command."""
    from server.git.filesystem import resolve_git_dir

    git_dir = resolve_git_dir(path)
    if not git_dir:
        return None

    # Try reading from config directly (no subprocess)
    url = parse_git_config_value(git_dir, "remote", "origin", "url")
    if url:
        return url

    # In worktrees, the config with remote URLs is in the common dir
    common_dir = get_common_dir(git_dir)
    if common_dir and common_dir != git_dir:
        url = parse_git_config_value(common_dir, "remote", "origin", "url")
        if url:
            return url

    return None


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


def normalize_git_remote_url(url: str) -> str | None:
    """Normalize git remote URL to canonical 'host/owner/repo' form.

    Handles SSH, HTTPS, SSH URL, git protocol, and localhost proxy formats.
    Returns lowercase normalized string, or None if URL can't be parsed.
    """
    trimmed = url.strip()
    if not trimmed:
        return None

    # Handle SSH format: git@host:owner/repo.git
    ssh_match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", trimmed)
    if ssh_match:
        return f"{ssh_match.group(1)}/{ssh_match.group(2)}".lower()

    # Handle HTTPS/SSH URL format: https://host/owner/repo.git or ssh://git@host/owner/repo
    url_match = re.match(r"^(?:https?|ssh|git)://(?:[^@]+@)?([^/]+)/(.+?)(?:\.git)?$", trimmed)
    if url_match:
        host = url_match.group(1)
        path = url_match.group(2)

        # CCR git proxy URLs: http://...@127.0.0.1:PORT/git/owner/repo
        if _is_localhost(host) and path.startswith("git/"):
            proxy_path = path[4:]  # Remove "git/" prefix
            segments = proxy_path.split("/")
            # 3+ segments where first contains a dot → host/owner/repo (GHE format)
            if len(segments) >= 3 and "." in segments[0]:
                return proxy_path.lower()
            # 2 segments → owner/repo (legacy format, assume github.com)
            return f"github.com/{proxy_path}".lower()

        return f"{host}/{path}".lower()

    return None


def get_is_clean(path: str) -> bool:
    """Check if working tree is clean (no uncommitted changes)."""
    try:
        result = _run_git(["--no-optional-locks", "status", "--porcelain"], cwd=path)
        return result.stdout.strip() == ""
    except Exception:
        return True


def get_file_status(path: str) -> dict:
    """Get tracked and untracked file lists.

    Returns: {"tracked": [...], "untracked": [...]}
    """
    try:
        result = _run_git(["--no-optional-locks", "status", "--porcelain"], cwd=path)
        tracked = []
        untracked = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            status = line[:2]
            filename = line[2:].strip()
            if status == "??":
                untracked.append(filename)
            elif filename:
                tracked.append(filename)
        return {"tracked": tracked, "untracked": untracked}
    except Exception:
        return {"tracked": [], "untracked": []}


# --- Private helpers ---


def _is_localhost(host: str) -> bool:
    """Check if host is localhost or 127.x.x.x."""
    host_no_port = host.split(":")[0]
    return host_no_port == "localhost" or bool(
        re.match(r"^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_no_port)
    )
