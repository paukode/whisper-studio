"""The folders no tool may delete, in any permission mode.

The hard block on deleting the filesystem root
(server.security.command_validator.check_root_deletes) and the ws_* file
tools (server.workspace.paths.ws_path_changeable) share this rule, so a
spelling the command parser reads and a path a file tool receives meet the
same line. Bypass lifts every approval card but this refusal.

A protected root is the filesystem root, a folder directly under it
(/System, /Users, /Applications, /usr, /private, ...), and the home folder
and every folder above it. A path below one of them (``~/Documents``,
``/tmp/build``) is not protected here; the permission mode decides about it.
"""

from __future__ import annotations

import os


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def is_protected_root(path: str) -> bool:
    """True when ``path`` names the filesystem root, a folder directly under
    it, or the home folder or one above it. Symlinks resolve first, so
    ``/tmp`` reads as ``/private/tmp`` (not protected) and ``/etc`` as
    ``/private/etc`` (not protected here; the sensitive-path list covers it)."""
    if not path:
        return False
    real = _real(path)
    if real == os.sep or os.path.dirname(real) == os.sep:
        return True
    home = _real("~")
    return home == real or home.startswith(real.rstrip(os.sep) + os.sep)
