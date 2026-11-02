"""FastAPI HTTP handlers for /api/workspace/*.

One submodule per concern. Each registers its handlers on the shared
``server.workspace.router`` with ``@router.*`` decorators; importing them here
(for that side effect only) wires the endpoints up. Nothing in this package
is part of the public API.

    connection      connect / disconnect / status
    browse          browse / mkdir / list-dir / search-files / pick-folder
    file_ops        read / source-file / write / delete / rename / duplicate / move / copy / undo
    edits           the stored edit behind one tool row (the diff dialog)
    search          file-history / grep
    shell           stop the work of a stopped turn
    os_integration  open-with / reveal
    recent          recent list read / remove / clear-unindexed
"""

from . import (  # noqa: F401  (imported for @router side-effects)
    browse,
    connection,
    edits,
    file_ops,
    os_integration,
    recent,
    search,
    shell,
)
