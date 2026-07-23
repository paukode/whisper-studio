"""
Core git utilities — git discovery, state queries, caching.

All functions are sync. Uses subprocess.run() for git commands
and direct file I/O for filesystem reads, matching Whisper's existing patterns.
"""

import hashlib
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


def find_canonical_git_root(start_path: str) -> str | None:
    """Resolve worktrees back to main repo identity.

    Follows .git file -> gitdir: -> commondir chain with security validation:
    1. worktreeGitDir is direct child of <commonDir>/worktrees/
    2. <worktreeGitDir>/gitdir points back to <gitRoot>/.git
    Both checks required to prevent trust-bypass attacks.
    """
    root = find_git_root(start_path)
    if not root:
        return None
    return _resolve_canonical_root(root)


def _resolve_canonical_root(git_root: str) -> str:
    """Resolve a git root to its canonical root (following worktree chain)."""
    try:
        git_file = os.path.join(git_root, ".git")
        # In a worktree, .git is a file containing: gitdir: <path>
        # In a regular repo, .git is a directory (open raises IsADirectoryError).
        with open(git_file) as f:
            git_content = f.read().strip()

        if not git_content.startswith("gitdir:"):
            return git_root

        worktree_git_dir = os.path.normpath(
            os.path.join(git_root, git_content[len("gitdir:") :].strip())
        )

        # commondir points to the shared .git directory (relative to worktree gitdir).
        # Submodules have no commondir — fall through.
        commondir_path = os.path.join(worktree_git_dir, "commondir")
        with open(commondir_path) as f:
            common_dir = os.path.normpath(os.path.join(worktree_git_dir, f.read().strip()))

        # SECURITY: Validate the structure matches what 'git worktree add' creates.
        #
        # 1. worktreeGitDir is a direct child of <commonDir>/worktrees/
        #    → ensures the commondir file we read lives inside the resolved
        #      common dir, not inside the attacker's repo
        if os.path.normpath(os.path.dirname(worktree_git_dir)) != os.path.join(
            common_dir, "worktrees"
        ):
            return git_root

        # 2. <worktreeGitDir>/gitdir points back to <gitRoot>/.git
        #    → ensures an attacker can't borrow a victim's existing worktree
        #      entry by guessing its path
        #
        # Git writes gitdir with strbuf_realpath() (symlinks resolved), but
        # gitRoot from findGitRoot() is only lexically resolved. Realpath gitRoot
        # so legitimate worktrees accessed via a symlinked path (e.g. macOS
        # /tmp → /private/tmp) aren't rejected.
        gitdir_path = os.path.join(worktree_git_dir, "gitdir")
        with open(gitdir_path) as f:
            backlink = os.path.realpath(f.read().strip())

        if backlink != os.path.join(os.path.realpath(git_root), ".git"):
            return git_root

        # Bare-repo worktrees: the common dir isn't inside a working directory.
        # Use the common dir itself as the stable identity.
        if os.path.basename(common_dir) != ".git":
            return unicodedata.normalize("NFC", common_dir)

        return unicodedata.normalize("NFC", os.path.dirname(common_dir))
    except (OSError, IsADirectoryError):
        return git_root


# --- State queries ---


def get_is_git(path: str) -> bool:
    """Check if path is inside a git repo."""
    return find_git_root(path) is not None


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


def get_cached_default_branch(path: str) -> str:
    from server.git.watcher import git_watcher

    return git_watcher.get(f"default_branch:{path}", lambda: get_default_branch(path))


def get_cached_remote_url(path: str) -> str | None:
    from server.git.watcher import git_watcher

    return git_watcher.get(f"remote_url:{path}", lambda: get_remote_url(path))


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


def get_is_clean(path: str, ignore_untracked: bool = False) -> bool:
    """Check if working tree is clean (no uncommitted changes)."""
    args = ["--no-optional-locks", "status", "--porcelain"]
    if ignore_untracked:
        args.append("-uno")
    try:
        result = _run_git(args, cwd=path)
        return result.stdout.strip() == ""
    except Exception:
        return True


def get_changed_files(path: str) -> list[str]:
    """List changed files (staged + unstaged)."""
    try:
        result = _run_git(["--no-optional-locks", "status", "--porcelain"], cwd=path)
        files = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line:
                # Remove status prefix (e.g. "M ", "A ", "??")
                parts = line.split(None, 1)
                if len(parts) >= 2:
                    files.append(parts[1].strip())
        return files
    except Exception:
        return []


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


def stash_to_clean_state(path: str, message: str | None = None) -> bool:
    """Stash all changes including untracked files.

    Stages untracked files before stashing to prevent data loss.
    """
    try:
        stash_message = message or f"Whisper auto-stash - {_iso_now()}"

        # Stage untracked files first
        status = get_file_status(path)
        if status["untracked"]:
            result = _run_git(["add", *status["untracked"]], cwd=path)
            if result.returncode != 0:
                return False

        # Stash everything
        result = _run_git(["stash", "push", "--message", stash_message], cwd=path)
        return result.returncode == 0
    except Exception:
        return False


def has_unpushed_commits(path: str) -> bool:
    """Check if local branch has commits not pushed to remote."""
    try:
        result = _run_git(["rev-list", "--count", "@{u}..HEAD"], cwd=path)
        return result.returncode == 0 and int(result.stdout.strip()) > 0
    except Exception:
        return False


def is_bare_git_repo(path: str) -> bool:
    """Detect if path looks like a bare/exploited git repo (security check).

    Detects sandbox escape attack vector where attacker creates HEAD, objects/,
    refs/, and hooks/pre-commit in cwd without a valid .git/HEAD.
    """
    git_path = os.path.join(path, ".git")
    try:
        os.stat(git_path)
        if os.path.isfile(git_path):
            # worktree/submodule — Git follows the gitdir reference
            return False
        if os.path.isdir(git_path):
            git_head = os.path.join(git_path, "HEAD")
            try:
                # SECURITY: check isFile(). An attacker creating .git/HEAD as a
                # DIRECTORY would pass a bare statSync but Git's setup_git_directory
                # rejects it (not a valid HEAD) and falls back to cwd discovery.
                if os.path.isfile(git_head):
                    # normal repo — .git/HEAD valid, Git won't fall back to cwd
                    return False
            except OSError:
                pass
            # .git exists but no HEAD or HEAD is not a file — fall through
    except OSError:
        pass

    # No valid .git/HEAD found. Check if path has bare git repo indicators.
    # Be cautious — flag if ANY of these exist without a valid .git reference.
    try:
        if os.path.isfile(os.path.join(path, "HEAD")):
            return True
    except OSError:
        pass
    try:
        if os.path.isdir(os.path.join(path, "objects")):
            return True
    except OSError:
        pass
    try:
        if os.path.isdir(os.path.join(path, "refs")):
            return True
    except OSError:
        pass
    return False


def get_repo_remote_hash(path: str) -> str | None:
    """SHA256 hash (first 16 chars) of normalized remote URL for unique repo identification."""
    remote_url = get_remote_url(path)
    if not remote_url:
        return None
    normalized = normalize_git_remote_url(remote_url)
    if not normalized:
        return None
    h = hashlib.sha256(normalized.encode()).hexdigest()
    return h[:16]


# --- Private helpers ---


def _is_localhost(host: str) -> bool:
    """Check if host is localhost or 127.x.x.x."""
    host_no_port = host.split(":")[0]
    return host_no_port == "localhost" or bool(
        re.match(r"^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_no_port)
    )


def _iso_now() -> str:
    """Return current UTC time in ISO format."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
