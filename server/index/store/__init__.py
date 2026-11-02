"""SQLite-backed workspace index store, in focused submodules that share one
connection factory and one in-process cache:

    base      _connect, caches, schema, write-gen, sqlite-vec search
    files     get_manifest / replace_file / touch_file / delete_file
    entities  description context + node dedup (lexical + semantic)
    meta      set/get meta, stats, remove_index, list_indexed_workspaces
    search    vector search (cached), GraphRAG hop, chunk fetch, FTS keyword
    graph     cached file-graph + explorer view wrappers over graph_views/explore_views

Every name external code (and graph_views) reads off ``server.index.store`` is
re-exported here: the public API plus the private helpers and constants other
modules reference (``_connect``, ``_invalidate``, ``_norm_entity``, the fanout
and generic-name constants) and ``db_path``, so ``store.<name>`` resolves.
"""

from __future__ import annotations

# Re-export the paths helper that callers reach as ``store.db_path``.
from ..paths import db_path  # noqa: F401
from .base import (  # noqa: F401
    _EDGE_ENTITY_SAMPLE,
    _GENERIC_ENTITY_NAMES,
    _MAX_NODE_FANOUT,
    _bump_write_gen,
    _connect,
    _fetch_chunks,
    _invalidate,
)
from .entities import (  # noqa: F401
    _norm_entity,
    dedupe_entities,
    entities_for_description,
    set_node_descriptions,
)
from .files import (  # noqa: F401
    delete_file,
    get_manifest,
    replace_file,
    touch_file,
)

# graph.py imports graph_views at its module bottom to close the
# graph_views<->store cycle; graph_views reads store attributes only at call
# time, so import order within this __init__ does not matter.
from .graph import (  # noqa: F401
    explore_entities,
    explore_entity,
    explore_file,
    explore_overview,
    file_graph,
)
from .meta import (  # noqa: F401
    get_meta,
    list_indexed_workspaces,
    remove_index,
    set_meta,
    stats,
)
from .search import (  # noqa: F401
    chunks_for_file,
    expand,
    fts_search,
    search,
)
