"""Where a deleting command reaches.

The rm floor (server.security.permissions.approval_floor) asks a person
before a command deletes anything the permission mode may not delete on its
own: anything outside the connected workspace and the app's own folders. A
delete that stays inside those roots is an ordinary command and follows the
mode, the session's answers and the rules like any other. This module reads
the operands of every deleting statement (server.security.command_validator.
delete_sites) and resolves them the way the shell would start to, against
the command's working directory, to say which side of that line they fall on.
It fails toward asking: an operand the command line alone cannot resolve (a
variable, a substitution, a relative path with no known working directory)
counts as outside.
"""

from __future__ import annotations

import os

from server.security.command_validator import delete_sites

_GLOB_CHARS = "*?[{"
_UNRESOLVABLE = ("$", "`")


def resolve_operand(operand: str, cwd: str | None) -> str | None:
    """The path an rm operand names, or None when the command line cannot say.

    ``~`` and ``~user`` expand the way the shell expands them. A glob is cut at
    its first wildcard and the directory before it is what gets checked, since
    everything the glob matches lives under that directory (``dist/*`` checks
    ``dist``, a bare ``*`` checks the working directory). The result is a real
    path, so a symlink inside the workspace that points elsewhere is read as
    elsewhere.
    """
    if not operand or any(c in operand for c in _UNRESOLVABLE):
        return None
    path = os.path.expanduser(operand)
    if path.startswith("~"):
        return None  # a user the system does not know
    cut = min((path.index(c) for c in _GLOB_CHARS if c in path), default=-1)
    if cut >= 0:
        path = path[:cut]
        path = path[: path.rfind("/") + 1]
    if not os.path.isabs(path):
        if not cwd:
            return None
        path = os.path.join(cwd, path)
    return os.path.realpath(os.path.normpath(path))


def _within(path: str, root: str) -> bool:
    real_root = os.path.realpath(root)
    return path == real_root or path.startswith(real_root.rstrip(os.sep) + os.sep)


def deletes_beyond(command: str, cwd: str | None, roots: list[str]) -> bool:
    """True when a deleting statement in ``command`` may touch anything outside
    ``roots``: there are no roots to be inside of, a delete's operands cannot
    be read or resolved, or one of them resolves elsewhere. False when the
    command deletes nothing, or deletes only under the roots."""
    sites = delete_sites(command)
    if not sites:
        return False
    roots = [r for r in roots if r]
    if not roots:
        return True
    for site in sites:
        if site.operands is None:
            return True
        for operand in site.operands:
            target = resolve_operand(operand, cwd)
            if target is None or not any(_within(target, r) for r in roots):
                return True
    return False
