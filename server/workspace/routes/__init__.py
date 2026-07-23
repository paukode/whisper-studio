"""FastAPI HTTP handlers for /api/workspace/*.

Split out of the former single ``routes.py`` module (1153 lines) into focused
submodules, one per concern. Each submodule registers its handlers on the
shared ``server.workspace.router`` via ``@router.*`` decorator side-effects;
importing them here (for their side-effects only) is what wires the endpoints
up. Nothing in this package is part of the public API — the package
``__init__`` imports it solely to trigger these registrations.

    connection      — connect / disconnect / status
    browse          — browse / mkdir / list-dir / search-files / pick-folder / worktrees
    file_ops        — read / source-file / write / delete / rename / duplicate / move / copy / undo
    search          — file-history / grep
    shell           — shell / shell task stop
    os_integration  — open-with / reveal
    recent          — recent list read / remove / clear-unindexed

The worktree route (``list_worktrees``) lives in ``browse`` — it shares the
same router and needs no module of its own.
"""

from . import (  # noqa: F401  (imported for @router side-effects)
    browse,
    connection,
    file_ops,
    os_integration,
    recent,
    search,
    shell,
)
