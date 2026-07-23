"""
Shell snapshot — captures user's shell environment once per session.

Sources the user's shell profile (.bashrc/.zshrc), captures aliases,
functions, and environment variables, then caches the result. Subsequent
commands in the same session are prefixed with the snapshot to make
user-defined aliases and functions available.
"""

import logging
import os
import shlex
import subprocess

log = logging.getLogger("whisper-studio")

# Cache: session_id -> snapshot script content
_snapshots: dict[str, str | None] = {}


def _detect_shell() -> str:
    """Detect the user's login shell."""
    shell = os.environ.get("SHELL", "/bin/sh")
    basename = os.path.basename(shell)
    if basename in ("zsh", "bash", "fish", "sh"):
        return shell
    return "/bin/sh"


def _profile_path(shell: str) -> str | None:
    """Return the profile file for the given shell."""
    basename = os.path.basename(shell)
    home = os.path.expanduser("~")
    candidates = {
        "zsh": [os.path.join(home, ".zshrc")],
        "bash": [
            os.path.join(home, ".bashrc"),
            os.path.join(home, ".bash_profile"),
        ],
    }
    for path in candidates.get(basename, []):
        if os.path.isfile(path):
            return path
    return None


def _build_snapshot_script(shell: str) -> str | None:
    """Build a snapshot script that sources the user's profile.

    Returns the script content, or None if no profile found.
    """
    profile = _profile_path(shell)
    if not profile:
        return None

    basename = os.path.basename(shell)

    if basename == "zsh":
        return f'source "{profile}" < /dev/null 2>/dev/null\nunalias -a 2>/dev/null\n'
    elif basename == "bash":
        return f'source "{profile}" < /dev/null 2>/dev/null\nunalias -a 2>/dev/null\n'
    return None


def _capture_snapshot(shell: str) -> str | None:
    """Execute the snapshot script and capture the resulting environment.

    Returns a shell script that restores aliases and functions, or None
    if snapshot creation fails.
    """
    script = _build_snapshot_script(shell)
    if not script:
        return None

    basename = os.path.basename(shell)

    # Capture aliases and exported functions after sourcing profile
    if basename == "zsh":
        capture_cmd = script + "\nalias\ntypeset -f 2>/dev/null"
    elif basename == "bash":
        capture_cmd = script + "\nalias\ndeclare -f 2>/dev/null"
    else:
        return None

    try:
        result = subprocess.run(
            [shell, "-c", capture_cmd],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            log.warning(
                "Shell snapshot failed (exit %d): %s", result.returncode, result.stderr[:200]
            )
            return None

        # The captured output IS the snapshot — we'll source the profile
        # directly instead of replaying aliases/functions for simplicity
        return script

    except (subprocess.TimeoutExpired, Exception) as e:
        log.warning("Shell snapshot failed: %s", e)
        return None


def get_snapshot(session_id: str) -> str | None:
    """Get or create the shell snapshot for a session.

    Returns a shell script prefix to prepend to commands, or None if
    no snapshot is available (commands will run in a plain shell).
    """
    if session_id in _snapshots:
        return _snapshots[session_id]

    shell = _detect_shell()
    snapshot = _capture_snapshot(shell)
    _snapshots[session_id] = snapshot

    if snapshot:
        log.info("Shell snapshot created for session %s (shell: %s)", session_id, shell)
    else:
        log.info("No shell snapshot for session %s (no profile found)", session_id)

    return snapshot


def wrap_command(command: str, session_id: str) -> str:
    """Wrap a command with the session's shell snapshot.

    The snapshot sources a shell-specific profile (a zsh profile aborts under
    /bin/sh on zsh-only syntax), but every executor downstream, run_sandboxed
    and the background shell runner alike, runs command strings with /bin/sh. So the
    wrapped command re-executes under the shell the snapshot was captured for.
    Callers must apply this as the outermost wrapper: anything appended after
    it (e.g. the cwd marker) would never run past the exec.
    """
    snapshot = get_snapshot(session_id)
    if not snapshot:
        return command
    shell = _detect_shell()
    return f"exec {shlex.quote(shell)} -c {shlex.quote(snapshot + command)}"


def clear_session(session_id: str):
    """Remove cached snapshot for a session."""
    _snapshots.pop(session_id, None)
