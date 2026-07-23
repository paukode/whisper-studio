"""
Filesystem-based git state reading — no subprocess spawning.

Reads .git/HEAD, refs, packed-refs, config, and commondir directly from disk.
Used on hot paths where subprocess overhead matters (prompt building, cache invalidation).
"""

import os
import re


def resolve_git_dir(start_path: str) -> str | None:
    """Resolve actual .git directory, handling worktrees and submodules.

    In a regular repo, .git is a directory. In worktrees/submodules,
    .git is a file containing 'gitdir: <path>'.
    """
    from server.git.core import find_git_root

    root = find_git_root(start_path)
    if not root:
        return None

    git_path = os.path.join(root, ".git")
    try:
        st = os.stat(git_path)
        if st.st_mode & 0o170000 == 0o100000:  # regular file
            # Worktree or submodule: .git is a file with 'gitdir: <path>'
            # Git strips trailing \n and \r (setup.c read_gitfile_gently).
            with open(git_path) as f:
                content = f.read().strip()
            if content.startswith("gitdir:"):
                raw_dir = content[len("gitdir:") :].strip()
                resolved = os.path.normpath(os.path.join(root, raw_dir))
                return resolved
        # Regular repo: .git is a directory
        return git_path
    except OSError:
        return None


def read_git_head(git_dir: str) -> dict | None:
    """Parse .git/HEAD to determine current branch or detached SHA.

    Returns:
        {"type": "branch", "name": "main"} for branch ref
        {"type": "detached", "sha": "abc123..."} for detached HEAD
        None on error or invalid content
    """
    try:
        with open(os.path.join(git_dir, "HEAD")) as f:
            content = f.read().strip()
    except OSError:
        return None

    if content.startswith("ref:"):
        ref = content[len("ref:") :].strip()
        if ref.startswith("refs/heads/"):
            name = ref[len("refs/heads/") :]
            # Reject path traversal and argument injection from a tampered HEAD.
            if not is_safe_ref_name(name):
                return None
            return {"type": "branch", "name": name}
        # Unusual symref (not a local branch) — resolve to SHA
        if not is_safe_ref_name(ref):
            return None
        sha = resolve_ref(git_dir, ref)
        return {"type": "detached", "sha": sha or ""}

    # Raw SHA (detached HEAD). Validate: an attacker-controlled HEAD file
    # could contain shell metacharacters that flow into downstream shell contexts.
    if not is_valid_git_sha(content):
        return None
    return {"type": "detached", "sha": content}


def resolve_ref(git_dir: str, ref: str) -> str | None:
    """Resolve git ref (e.g. 'refs/heads/main') to commit SHA.

    Checks loose ref files first, then packed-refs.
    For worktrees, falls back to common gitdir where shared refs live.
    """
    result = _resolve_ref_in_dir(git_dir, ref)
    if result:
        return result

    # For worktrees: try the common gitdir where shared refs live
    common_dir = get_common_dir(git_dir)
    if common_dir and common_dir != git_dir:
        return _resolve_ref_in_dir(common_dir, ref)

    return None


def _resolve_ref_in_dir(dir_path: str, ref: str) -> str | None:
    """Resolve ref in a specific directory. Tries loose ref file, then packed-refs."""
    # Try loose ref file
    try:
        with open(os.path.join(dir_path, ref)) as f:
            content = f.read().strip()
        if content.startswith("ref:"):
            target = content[len("ref:") :].strip()
            # Reject path traversal in a tampered symref chain.
            if not is_safe_ref_name(target):
                return None
            return resolve_ref(dir_path, target)
        # Loose ref content should be a raw SHA. Validate: an attacker-controlled
        # ref file could contain shell metacharacters.
        if not is_valid_git_sha(content):
            return None
        return content
    except OSError:
        pass

    # Try packed-refs
    try:
        with open(os.path.join(dir_path, "packed-refs")) as f:
            packed = f.read()
        for line in packed.split("\n"):
            if line.startswith("#") or line.startswith("^"):
                continue
            space_idx = line.find(" ")
            if space_idx == -1:
                continue
            if line[space_idx + 1 :] == ref:
                sha = line[:space_idx]
                return sha if is_valid_git_sha(sha) else None
    except OSError:
        pass

    return None


def is_safe_ref_name(name: str) -> bool:
    """Validate ref/branch names are safe for path joins, git arguments, and shell commands.

    Allowlist: ASCII alphanumerics, /, ., _, +, -, @ only.
    Rejects path traversal (..), leading dash, shell metacharacters, whitespace, NUL,
    non-ASCII, empty path components, and single-dot components.
    """
    if not name or name.startswith("-") or name.startswith("/"):
        return False
    if ".." in name:
        return False
    # Reject single-dot and empty path components (., foo/./bar, foo//bar, foo/).
    # Git-check-ref-format rejects these, and . normalizes away in path joins.
    if any(c == "." or c == "" for c in name.split("/")):
        return False
    # Allowlist-only: alphanumerics, /, ., _, +, -, @. Rejects all shell
    # metacharacters, whitespace, NUL, and non-ASCII. Git's forbidden @{
    # sequence is blocked because { is not in the allowlist.
    if not re.match(r"^[a-zA-Z0-9/._+@-]+$", name):
        return False
    return True


def is_valid_git_sha(s: str) -> bool:
    """Validate git SHA: 40 hex chars (SHA-1) or 64 hex chars (SHA-256).

    Only accepts full-length hashes — rejects abbreviated SHAs.
    """
    return bool(re.match(r"^[0-9a-f]{40}$", s) or re.match(r"^[0-9a-f]{64}$", s))


def get_common_dir(git_dir: str) -> str | None:
    """Read commondir file to find shared git directory.

    In a worktree, commondir points to the main repo's .git dir.
    Returns None if no commondir file (regular repo).
    """
    try:
        with open(os.path.join(git_dir, "commondir")) as f:
            content = f.read().strip()
        return os.path.normpath(os.path.join(git_dir, content))
    except OSError:
        return None


def is_shallow_clone(git_dir: str) -> bool:
    """Check if repo is a shallow clone by looking for shallow file.

    Mere existence of <commonDir>/shallow means shallow (per git's shallow.c).
    """
    common_dir = get_common_dir(git_dir) or git_dir
    return os.path.isfile(os.path.join(common_dir, "shallow"))


def read_worktree_head_sha(worktree_path: str) -> str | None:
    """Fast HEAD read for a git worktree directory.

    Reads <worktreePath>/.git directly as gitdir: pointer file with no upward walk.
    ~15ms faster than subprocess git rev-parse HEAD.
    """
    try:
        with open(os.path.join(worktree_path, ".git")) as f:
            ptr = f.read().strip()
        if not ptr.startswith("gitdir:"):
            return None
        git_dir = os.path.normpath(os.path.join(worktree_path, ptr[len("gitdir:") :].strip()))
    except OSError:
        return None

    head = read_git_head(git_dir)
    if not head:
        return None
    if head["type"] == "branch":
        return resolve_ref(git_dir, f"refs/heads/{head['name']}")
    return head.get("sha")


def get_worktree_count(git_dir: str) -> int:
    """Count worktrees by reading <commonDir>/worktrees/ directory.

    Main worktree not listed in the directory, so add 1.
    """
    try:
        common_dir = get_common_dir(git_dir) or git_dir
        entries = os.listdir(os.path.join(common_dir, "worktrees"))
        return len(entries) + 1
    except OSError:
        # No worktrees directory means only the main worktree
        return 1
