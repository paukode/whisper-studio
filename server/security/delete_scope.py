"""Where a deleting command reaches.

The rm floor (server.security.permissions.approval_floor) asks a person
before a command deletes anything the permission mode may not delete on its
own: anything outside the connected workspace and the app's own folders. A
delete that stays inside those roots is an ordinary command and follows the
mode, the session's answers and the rules like any other. This module reads
the operands of every deleting statement (server.security.delete_sites)
and resolves them the way the shell would start to, against
the command's working directory and the ``cd``s before them, to say which
side of that line they fall on. It fails toward asking: an operand the
command line alone cannot resolve (a variable, a substitution, a relative
path with no known working directory, or one after a ``cd`` it cannot
follow) counts as outside.
"""

from __future__ import annotations

import os

from server.security.delete_sites import ChDir, delete_sites

_GLOB_CHARS = "*?[{"
_UNRESOLVABLE = ("$", "`")
# More working directories than this for one delete (a long chain of cds
# that each may or may not happen) reads as no known working directory.
_MAX_WORKING_DIRS = 64


def resolve_operand(operand: str, cwd: str | None) -> str | None:
    """The path an rm operand names, or None when the command line cannot say.

    ``~`` and ``~user`` expand the way the shell expands them. A glob is cut at
    its first wildcard and the directory before it is what gets checked, since
    everything the glob matches lives under that directory (``dist/*`` checks
    ``dist``, a bare ``*`` checks the working directory). A relative operand
    starts from the real path of the working directory, as the kernel starts
    from it: a working directory reached through a symlink is where the link
    points, so ``..`` climbs from there. The result is a real path, so a
    symlink inside the workspace that points elsewhere is read as elsewhere,
    and a ``..`` after it climbs from where it points, as the kernel climbs
    (``link/../x`` is beside the link's target, not beside the link).
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
        path = os.path.join(os.path.realpath(cwd), path)
    # realpath alone: a normpath first would drop link/.. off the path as
    # written, before the link was followed.
    return os.path.realpath(path)


def _change_dir(cwd: str | None, target: str | None) -> list[str | None]:
    """Where ``cd target`` run in ``cwd`` lands, or [None] when the command
    line cannot say: no single target (delete_sites._cd_target), a
    variable, a substitution, a glob, an unknown ``~user``, or a relative
    target with no known working directory. ``~`` expands as it does for an
    operand. A symlink can make two landings differ, and both are kept: the
    one cd takes by default, with ``..`` read off the path as written, and the
    real path that ``cd -P`` or a shell in physical mode takes."""
    if not target or any(c in target for c in (*_UNRESOLVABLE, *_GLOB_CHARS)):
        return [None]
    path = os.path.expanduser(target)
    if path.startswith("~"):
        return [None]
    if not os.path.isabs(path):
        if not cwd:
            return [None]
        path = os.path.join(cwd, path)
    return [os.path.normpath(path), os.path.realpath(path)]


def _working_dirs(cwd: str | None, chdirs: tuple[ChDir, ...]) -> list[str | None]:
    """Every directory a delete that follows ``chdirs`` may run in. A sure cd
    (ChDir.sure) replaces the directories possible before it with where it
    lands; any other cd may not have taken effect, so it adds where it lands
    to them. None stands for a directory the command line cannot name."""
    dirs: list[str | None] = [cwd]
    for change in chdirs:
        landed = [d for here in dirs for d in _change_dir(here, change.target)]
        dirs = list(dict.fromkeys(landed if change.sure else dirs + landed))
        if len(dirs) > _MAX_WORKING_DIRS:
            return [None]
    return dirs


def _within(path: str, root: str) -> bool:
    real_root = os.path.realpath(root)
    return path == real_root or path.startswith(real_root.rstrip(os.sep) + os.sep)


def deletes_beyond(command: str, cwd: str | None, roots: list[str]) -> bool:
    """True when a deleting statement in ``command`` may touch anything outside
    ``roots``: there are no roots to be inside of, a delete's operands cannot
    be read or resolved, or one of them resolves elsewhere from any directory
    the delete may run in (``cwd``, or where a ``cd`` before it moves). False
    when the command deletes nothing, or deletes only under the roots."""
    sites = delete_sites(command)
    if not sites:
        return False
    roots = [r for r in roots if r]
    if not roots:
        return True
    for site in sites:
        if site.operands is None:
            return True
        dirs = _working_dirs(cwd, site.chdirs)
        for operand in site.operands:
            for here in dirs:
                target = resolve_operand(operand, here)
                if target is None or not any(_within(target, r) for r in roots):
                    return True
    return False
