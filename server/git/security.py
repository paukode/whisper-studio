"""
Security layer — defense-in-depth checks for git operations.

Bare repo detection, worktree validation, ref name validation,
destructive command detection, read-only command validation,
safety protocol enforcement, and secret file detection.
"""

import fnmatch
import os
import re

# --- Read-only command allowlist ---

GIT_READ_ONLY_COMMANDS = {
    "status",
    "log",
    "diff",
    "show",
    "blame",
    "branch --list",
    "branch -a",
    "branch -r",
    "remote -v",
    "remote show",
    "tag -l",
    "tag --list",
    "stash list",
    "shortlog",
    "describe",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "cat-file",
    "check-ignore",
    "name-rev",
    "reflog",
}

# --- Blocked flags and subcommands ---

GIT_BLOCKED_FLAGS = {"--no-verify", "--no-gpg-sign", "-i", "--interactive"}
GIT_BLOCKED_SUBCOMMANDS = {"config"}  # Block git config writes

# --- Destructive command patterns ---

_DESTRUCTIVE_PATTERNS = [
    {
        "pattern": r"push\s+.*(-f|--force)",
        "warning": "Force push can overwrite remote history",
    },
    {
        "pattern": r"reset\s+--hard",
        "warning": "Hard reset discards all uncommitted changes",
    },
    {
        "pattern": r"checkout\s+\.",
        "warning": "Discards all unstaged changes",
    },
    {
        "pattern": r"restore\s+\.",
        "warning": "Discards all unstaged changes",
    },
    {
        "pattern": r"clean\s+.*-f",
        "warning": "Permanently deletes untracked files",
    },
    {
        "pattern": r"branch\s+.*-D",
        "warning": "Force-deletes branch even if not merged",
    },
    {
        "pattern": r"push\s+.*(-f|--force).*(main|master)",
        "warning": "Force pushing to main/master can break shared history",
    },
    {
        "pattern": r"push\s+.*(main|master).*(-f|--force)",
        "warning": "Force pushing to main/master can break shared history",
    },
]

# --- Secret file patterns ---

SECRET_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "credentials.json",
    "service-account*.json",
    "*secret*",
    "*password*",
    "*.keystore",
    "id_rsa",
    "id_ed25519",
    "*.pub",
]


# --- Public API ---


def check_bare_repo(path: str) -> bool:
    """Detect bare repo attacks: HEAD + objects/ + refs/ without valid .git/HEAD.

    This is a security check — a bare/exploited git directory can be used
    for sandbox escape attacks where an attacker creates HEAD, objects/,
    refs/, and hooks/pre-commit in cwd.
    """
    from server.git.core import is_bare_git_repo

    return is_bare_git_repo(path)


def validate_worktree(git_root: str) -> bool:
    """Validate worktree commondir chain with back-link validation.

    Checks:
    1. worktreeGitDir is direct child of <commonDir>/worktrees/
    2. <worktreeGitDir>/gitdir points back to <gitRoot>/.git
    3. Realpath comparison to handle symlinked paths

    Returns True if valid (or if not a worktree). False if validation fails.
    """
    git_file = os.path.join(git_root, ".git")

    # Regular repo (not a worktree) — always valid
    if os.path.isdir(git_file):
        return True

    # Not a git repo at all
    if not os.path.isfile(git_file):
        return False

    try:
        with open(git_file) as f:
            content = f.read().strip()

        if not content.startswith("gitdir:"):
            return False

        worktree_git_dir = os.path.normpath(
            os.path.join(git_root, content[len("gitdir:") :].strip())
        )

        # Read commondir
        commondir_path = os.path.join(worktree_git_dir, "commondir")
        with open(commondir_path) as f:
            common_dir = os.path.normpath(os.path.join(worktree_git_dir, f.read().strip()))

        # Check 1: worktreeGitDir is direct child of <commonDir>/worktrees/
        if os.path.normpath(os.path.dirname(worktree_git_dir)) != os.path.join(
            common_dir, "worktrees"
        ):
            return False

        # Check 2: backlink points to <gitRoot>/.git
        gitdir_path = os.path.join(worktree_git_dir, "gitdir")
        with open(gitdir_path) as f:
            backlink = os.path.realpath(f.read().strip())

        if backlink != os.path.join(os.path.realpath(git_root), ".git"):
            return False

        return True
    except OSError:
        return False


def get_destructive_command_warning(command: str) -> str | None:
    """Returns warning string for dangerous git operations, None if safe.

    Checks against known destructive patterns like force push,
    hard reset, checkout ., clean -f, branch -D.
    """
    # Strip 'git ' prefix if present
    cmd = command.strip()
    if cmd.startswith("git "):
        cmd = cmd[4:]

    for entry in _DESTRUCTIVE_PATTERNS:
        if re.search(entry["pattern"], cmd):
            return entry["warning"]
    return None


def is_read_only_git_command(command: str) -> bool:
    """Check if a git command is in the read-only allowlist.

    Safe for auto-approval — these commands don't modify repo state.
    """
    cmd = command.strip()
    if cmd.startswith("git "):
        cmd = cmd[4:]
    cmd = cmd.strip()

    # Check exact matches and prefix matches
    for allowed in GIT_READ_ONLY_COMMANDS:
        if cmd == allowed or cmd.startswith(allowed + " "):
            return True

    # Also check just the subcommand (e.g. "status" matches "status --porcelain")
    subcommand = cmd.split()[0] if cmd else ""
    read_only_subcommands = {
        "status",
        "log",
        "diff",
        "show",
        "blame",
        "shortlog",
        "describe",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "cat-file",
        "check-ignore",
        "name-rev",
        "reflog",
    }
    return subcommand in read_only_subcommands


def validate_git_command(command: str) -> dict:
    """Full validation of a git command.

    Returns: {"allowed": bool, "warning": str | None, "reason": str}
    """
    cmd = command.strip()
    if cmd.startswith("git "):
        cmd = cmd[4:]
    cmd = cmd.strip()

    # Check blocked subcommands
    subcommand = cmd.split()[0] if cmd else ""
    if subcommand in GIT_BLOCKED_SUBCOMMANDS:
        # Allow read-only config operations
        if subcommand == "config" and any(
            flag in cmd for flag in ("--get", "--list", "-l", "--get-all")
        ):
            pass  # Read-only config is fine
        else:
            return {
                "allowed": False,
                "warning": None,
                "reason": f"git {subcommand} writes are blocked — never modify git config programmatically",
            }

    # Check blocked flags
    parts = cmd.split()
    for flag in GIT_BLOCKED_FLAGS:
        if flag in parts:
            return {
                "allowed": False,
                "warning": None,
                "reason": f"Flag '{flag}' is blocked — never skip hooks or use interactive mode",
            }

    # Check for --amend without explicit context
    if "--amend" in parts and subcommand == "commit":
        return {
            "allowed": False,
            "warning": "Amending can destroy the previous commit",
            "reason": "commit --amend is blocked unless explicitly requested by user",
        }

    # Check destructive patterns
    warning = get_destructive_command_warning(cmd)
    if warning:
        return {
            "allowed": True,
            "warning": warning,
            "reason": "Destructive operation detected",
        }

    return {"allowed": True, "warning": None, "reason": ""}


def contains_secret_files(file_list: list[str]) -> list[str]:
    """Check a list of file paths for potential secret files.

    Returns list of matched secret file paths (empty if none found).
    """
    secrets = []
    for filepath in file_list:
        basename = os.path.basename(filepath)
        for pattern in SECRET_PATTERNS:
            if fnmatch.fnmatch(basename, pattern):
                secrets.append(filepath)
                break
    return secrets
