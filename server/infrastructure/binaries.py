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


def enrich_gui_launch_path() -> None:
    """Widen ``PATH`` for a Finder/launchd-launched .app.

    A GUI-launched app inherits a minimal PATH with none of the user's shell
    config, so user-configured MCP server commands (``uvx``, ``npx``,
    ``python3``, ``docker``, …) and other spawned tools fail with
    ``[Errno 2] No such file or directory``. Enrich PATH from the user's real
    tool locations: their login-shell PATH (which encodes nvm / asdf / pyenv /
    Homebrew as configured) plus the well-known dirs above as a fallback.

    Packaged mode only — keyed on ``WHISPER_HOME`` — so a dev checkout, which
    already has a full PATH, is untouched. Best-effort and time-bounded: any
    failure leaves PATH as it was. The bundle's own ``WHISPER_BIN_DIR`` stays
    first so bundled binaries (node, ffmpeg, llama-server) remain authoritative.
    """
    if not os.environ.get("WHISPER_HOME", "").strip():
        return

    discovered: list[str] = []

    # 1) The user's login shell PATH. `-l` sources login files, `-i` the
    #    interactive rc (where PATH is often set); `printf` avoids prompt noise.
    shell = os.environ.get("SHELL", "/bin/zsh")
    try:
        out = subprocess.run(
            [shell, "-lic", 'printf "%s" "$PATH"'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for d in out.stdout.strip().split(os.pathsep):
            if d and d not in discovered:
                discovered.append(d)
    except Exception as e:  # timeout, missing shell, weird rc — fall back below
        log.debug("login-shell PATH capture failed: %s", e)

    # 2) Well-known dirs that actually exist (supplements a thin/failed capture).
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
