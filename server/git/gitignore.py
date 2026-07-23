"""
Gitignore handling — check if paths are ignored, manage global gitignore rules.
"""

import os
import subprocess


def is_path_gitignored(file_path: str, cwd: str) -> bool:
    """Check if a path is ignored by git via 'git check-ignore'.

    Consults all applicable gitignore sources: repo .gitignore files (nested),
    .git/info/exclude, global gitignore — with correct precedence.

    Exit codes: 0=ignored, 1=not ignored, 128=not in git repo (returns False).
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", file_path],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_global_gitignore_path() -> str:
    """Get path to global gitignore file (~/.config/git/ignore)."""
    return os.path.join(os.path.expanduser("~"), ".config", "git", "ignore")


def add_file_glob_rule(pattern: str, cwd: str | None = None) -> None:
    """Add a file pattern to global gitignore if not already ignored.

    Creates the global gitignore file and parent directories if needed.
    Skips silently if pattern is already ignored or if not in a git repo.
    """
    try:
        # Check if directory is in a git repo
        if cwd:
            from server.git.core import find_git_root

            if not find_git_root(cwd):
                return

        gitignore_entry = f"**/{pattern}"

        # Check if already ignored
        if cwd:
            test_path = f"{pattern}sample-file.txt" if pattern.endswith("/") else pattern
            if is_path_gitignored(test_path, cwd):
                return

        global_path = get_global_gitignore_path()

        # Create directory if needed
        config_git_dir = os.path.dirname(global_path)
        os.makedirs(config_git_dir, exist_ok=True)

        # Check if pattern already exists in global gitignore
        try:
            with open(global_path) as f:
                content = f.read()
            if gitignore_entry in content:
                return
            with open(global_path, "a") as f:
                f.write(f"\n{gitignore_entry}\n")
        except FileNotFoundError:
            with open(global_path, "w") as f:
                f.write(f"{gitignore_entry}\n")
    except Exception:
        pass
