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

import os
import shutil


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
