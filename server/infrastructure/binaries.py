"""Resolve the external binaries the server spawns (llama-server, ffmpeg, …).

A packaged .app launched by Finder/launchd inherits a minimal PATH (no
/opt/homebrew/bin, no nvm shims), so every spawn point routes through
:func:`resolve` and can be pinned explicitly via an environment variable set by
the bundle's launcher. In a dev checkout nothing changes: with no env override
the PATH lookup finds the same binary a bare ``subprocess.run(["name", ...])``
would have.

Resolution order:
  1. the ``env_var`` override (must point at an executable file)
  2. ``shutil.which(name)``
  3. ``fallbacks`` — directories to probe for ``name``
"""

import logging
import os
import shutil
import subprocess

log = logging.getLogger("whisper-studio")

# Well-known locations for user-installed CLIs, probed when the login-shell
# capture fails or comes back thin. Covers Homebrew (arm64 + intel), pipx /
# `pip install --user`, and Rust/cargo tools.
_COMMON_TOOL_DIRS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "~/.local/bin",
    "~/.cargo/bin",
)


# The user's login-shell environment, captured once at packaged startup by
# load_login_shell_env(). Empty in a dev checkout, whose process was started
# from a terminal and already carries that environment.
_LOGIN_ENV: dict[str, str] = {}

# Shell bookkeeping that describes the capture shell itself, not the user.
_SHELL_STATE_VARS = frozenset({"PWD", "OLDPWD", "SHLVL", "_"})

_ENV_MARKER = "__WHISPER_LOGIN_ENV__"


def _capture_login_shell_env() -> dict[str, str]:
    """Run the user's login shell once and read back its environment.

    ``-l`` sources the login files and ``-i`` the interactive rc, where most
    users export PATH, AWS_PROFILE and tool settings. Markers fence the
    ``env -0`` output so banner text printed by an rc file cannot corrupt it,
    and NUL separators keep multi-line values intact.
    """
    shell = os.environ.get("SHELL", "/bin/zsh")
    script = f'printf "%s" {_ENV_MARKER}; /usr/bin/env -0; printf "%s" {_ENV_MARKER}'
    out = subprocess.run(
        [shell, "-lic", script],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=5,
    )
    raw = out.stdout.decode("utf-8", errors="replace")
    first = raw.find(_ENV_MARKER)
    last = raw.rfind(_ENV_MARKER)
    if first < 0 or last <= first:
        return {}
    env: dict[str, str] = {}
    for entry in raw[first + len(_ENV_MARKER) : last].split("\0"):
        key, sep, value = entry.partition("=")
        if sep and key.isidentifier() and key not in _SHELL_STATE_VARS:
            env[key] = value
    return env


def load_login_shell_env() -> None:
    """Adopt the user's shell environment for a Finder/launchd-launched .app.

    A GUI-launched app inherits a nearly empty environment with none of the
    user's shell config, while Claude Code and other CLIs start from a
    terminal and get all of it. Capture the login shell's environment once:

      - PATH is widened in this process so user-configured commands (``uvx``,
        ``npx``, ``docker``, …) resolve, with the well-known tool dirs above
        filling a thin or failed capture.
      - The full environment is kept for MCP server children
        (:func:`login_shell_env`), so a server sees the same AWS_PROFILE,
        AWS_REGION and tool settings it would in a terminal.

    Packaged mode only, keyed on ``WHISPER_HOME``: a dev checkout already runs
    with the terminal's environment. Best-effort and time-bounded: a failed
    capture leaves the environment as it was. The bundle's own
    ``WHISPER_BIN_DIR`` stays first so bundled binaries (node, ffmpeg,
    llama-server) remain authoritative.
    """
    if not os.environ.get("WHISPER_HOME", "").strip():
        return

    try:
        captured = _capture_login_shell_env()
    except Exception as e:  # timeout, missing shell, weird rc
        log.debug("login-shell environment capture failed: %s", e)
        captured = {}
    _LOGIN_ENV.clear()
    _LOGIN_ENV.update(captured)
    log.info("Login-shell environment captured (%d variables)", len(captured))

    discovered: list[str] = []
    for d in captured.get("PATH", "").split(os.pathsep):
        if d and d not in discovered:
            discovered.append(d)
    # Well-known dirs that actually exist (supplements a thin/failed capture).
    for d in _COMMON_TOOL_DIRS:
        expanded = os.path.expanduser(d)
        if os.path.isdir(expanded) and expanded not in discovered:
            discovered.append(expanded)

    if not discovered:
        return

    bin_dir = os.environ.get("WHISPER_BIN_DIR", "").strip()
    existing = os.environ.get("PATH", "").split(os.pathsep)
    merged: list[str] = []
    # WHISPER_BIN_DIR first (bundled binaries win), then user tools, then the
    # inherited minimal PATH.
    for d in ([bin_dir] if bin_dir else []) + discovered + existing:
        if d and d not in merged:
            merged.append(d)
    os.environ["PATH"] = os.pathsep.join(merged)
    log.info("PATH enriched for GUI launch (%d entries)", len(merged))


def login_shell_env() -> dict[str, str]:
    """The captured login-shell environment (empty in a dev checkout)."""
    return dict(_LOGIN_ENV)


def resolve(name: str, env_var: str = "", fallbacks: tuple[str, ...] = ()) -> str | None:
    """Absolute path to ``name``, or None when it can't be found."""
    if env_var:
        override = os.environ.get(env_var, "").strip()
        if override:
            candidate = os.path.abspath(os.path.expanduser(override))
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    found = shutil.which(name)
    if found:
        return found
    for d in fallbacks:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None
