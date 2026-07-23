"""Connection, caches, schema, and low-level vector primitives shared by every
store submodule.

The SQLite connection factory (``_connect``), the in-process caches
(``_VEC_CACHE`` / ``_DERIVED_CACHE``), the sqlite-vec probe (``_vec_loadable``),
and the brute-force matrix builder (``_matrix``) all live here so the topical
modules (files, entities, meta, search, graph) share one cache and one write-gen
protocol. Everything else imports these from here rather than duplicating state.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading

import numpy as np

from ..config import (
    EMBED_DIM,
    dim_for_backend,
)
from ..paths import db_path, workspace_index_dir


def _active_dim() -> int:
    """Embedding width for the active embed backend. Each per-backend index DB
    holds vectors of exactly one width, and db_path routes to that backend's DB,
    so the dim used for the matrix / sqlite-vec table must match it."""
    try:
        from server.infrastructure.model_mode import resolve_backend

        return dim_for_backend(resolve_backend("embed"))
    except Exception:
        return EMBED_DIM


log = logging.getLogger("whisper-studio")

# Cached (chunk_ids, matrix) per workspace db path; invalidated on write.
_VEC_CACHE: dict[str, tuple[list[int], np.ndarray]] = {}
# Recomputable derived views (search hits + graph/projection/community layouts)
# memoized per (db_path, key). Purged alongside _VEC_CACHE on any index write
# (see _invalidate), so a stale view is never returned; capped to bound memory.
_DERIVED_CACHE: dict[tuple, object] = {}
_DERIVED_CACHE_MAX = 256
_cache_lock = threading.Lock()

# Vector search backend: sqlite-vec (indexed KNN) when the interpreter can load
# SQLite extensions — true on a Homebrew-Python venv, which setup.sh builds; the
# python.org framework build cannot, so we fall back to a brute-force numpy
# matrix. Probed once per process.
_VEC_OK: bool | None = None

# Entities appearing in more chunks than this are treated as boilerplate and
# excluded from file_graph edges (they'd blow up the self-join and aren't a
# meaningful relationship signal).
_MAX_NODE_FANOUT = 60

# How many of an edge's shared entity names to surface for display (the "why
# are these two files linked" detail). The full count is still the edge weight.
_EDGE_ENTITY_SAMPLE = 8

# For the unified (all-workspaces) graph: an entity appearing in more files than
# this across the whole corpus is boilerplate and excluded from edges.
_MAX_FILE_FANOUT = 25

# Salience filter: generic category words ("function", "service", "concept", …)
# that GLiNER faithfully tags as entities but that carry no relationship signal.
# Excluded from co-occurrence edges so they don't clutter the graph or inflate
# edge weights. Lower-cased; matched against the entity name.
_GENERIC_ENTITY_NAMES = {
    "person",
    "people",
    "organization",
    "organisation",
    "org",
    "product",
    "technology",
    "programming language",
    "language",
    "library",
    "framework",
    "module",
    "function",
    "method",
    "class",
    "api",
    "apis",
    "service",
    "services",
    "database",
    "concept",
    "concepts",
    "file",
    "files",
    "protocol",
    "event",
    "events",
    "location",
    "object",
    "type",
    "string",
    "variable",
    "parameter",
    "value",
}

# Per-label cap on the semantic-merge cosine (which is O(n²) within a label):
# labels with more distinct entities than this skip the semantic pass. The
# lexical pass still applies to them.
_SEMANTIC_MERGE_MAX = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, hash TEXT, size INTEGER, mtime REAL, n_chunks INTEGER
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, start_line INTEGER,
    end_line INTEGER, text TEXT, vec BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, label TEXT, UNIQUE(name, label)
);
CREATE TABLE IF NOT EXISTS node_chunks (
    node_id INTEGER, chunk_id INTEGER, PRIMARY KEY(node_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_nc_chunk ON node_chunks(chunk_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- Typed entity↔entity relations (optional; only populated when the typed-
-- relations feature is enabled). Keyed by entity NAME (not node id) so dedupe
-- can't dangle them; scoped by source file ``path`` so re-indexing one file
-- replaces just its relations.
CREATE TABLE IF NOT EXISTS relations (
    source TEXT, target TEXT, type TEXT, path TEXT,
    PRIMARY KEY(source, target, type, path)
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relations(source);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relations(target);
-- Typed relations v2: keyed by NODE ID (survives entity merges via repointing),
-- carrying verbatim evidence + line provenance so a fact is citable, with
-- evidence_hash collapsing the same fact re-stated across duplicate files.
CREATE TABLE IF NOT EXISTS relations2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_node_id INTEGER NOT NULL, tgt_node_id INTEGER NOT NULL,
    predicate TEXT NOT NULL, strength REAL NOT NULL DEFAULT 3.0,
    path TEXT NOT NULL, start_line INTEGER, end_line INTEGER, evidence TEXT,
    evidence_hash TEXT NOT NULL,
    UNIQUE(src_node_id, predicate, tgt_node_id, evidence_hash)
);
CREATE INDEX IF NOT EXISTS idx_rel2_src ON relations2(src_node_id);
CREATE INDEX IF NOT EXISTS idx_rel2_tgt ON relations2(tgt_node_id);
CREATE INDEX IF NOT EXISTS idx_rel2_path ON relations2(path);
"""


def _connect(ws_path: str) -> sqlite3.Connection:
    os.makedirs(workspace_index_dir(ws_path), exist_ok=True)
    conn = sqlite3.connect(db_path(ws_path))
    # A background index build writes in its own thread; without a busy timeout a
    # concurrent reader (vector/keyword search) fails instantly with "database is
    # locked" and silently degrades. Retry briefly instead.
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(_SCHEMA)
    # Columns added after the original schema. ALTER on an already-present column
    # raises OperationalError, which we ignore — keeps this idempotent and lets
    # older index DBs upgrade in place without a rebuild.
    for _stmt in (
        "ALTER TABLE nodes ADD COLUMN description TEXT",
        "ALTER TABLE relations ADD COLUMN score REAL",
        # Entity salience layer (statistical noise defense). salience NULL means
        # "not yet computed" — readers COALESCE it to a neutral 0.5 so old index
        # DBs behave as before until the next build backfills. canonical_id NULL
        # means self-canonical; it is reserved for the LLM canonicalization pass
        # and is unset in the statistical-only path.
        "ALTER TABLE nodes ADD COLUMN salience REAL",
        "ALTER TABLE nodes ADD COLUMN df INTEGER",
        "ALTER TABLE nodes ADD COLUMN canonical_id INTEGER",
        "ALTER TABLE nodes ADD COLUMN llm_verdict TEXT",
        "ALTER TABLE nodes ADD COLUMN judged_gen INTEGER",
        # GLiNER span confidence per (node, chunk) mention — averaged into salience.
        "ALTER TABLE node_chunks ADD COLUMN score REAL",
    ):
        try:
            conn.execute(_stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def _invalidate(ws_path: str) -> None:
    with _cache_lock:
        dp = db_path(ws_path)
        _VEC_CACHE.pop(dp, None)
        for k in [k for k in _DERIVED_CACHE if k[0] == dp]:
            _DERIVED_CACHE.pop(k, None)


def _cached_derived(ws_path: str, key: tuple, compute):
    """Memoize a recomputable derived view (search / graph / projection) keyed by
    (db_path, key). Every index write calls _invalidate(), which purges these, so
    a stale view is never returned. Callers must treat the result as READ-ONLY
    (it is shared); all current callers serialize it to JSON without mutating."""
    ck = (db_path(ws_path), key)
    with _cache_lock:
        if ck in _DERIVED_CACHE:
            return _DERIVED_CACHE[ck]
    val = compute()
    with _cache_lock:
        if len(_DERIVED_CACHE) >= _DERIVED_CACHE_MAX:
            _DERIVED_CACHE.pop(next(iter(_DERIVED_CACHE)), None)
        _DERIVED_CACHE[ck] = val
    return val


def _delete_file_rows(cur: sqlite3.Cursor, path: str) -> None:
    ids = [r[0] for r in cur.execute("SELECT id FROM chunks WHERE path=?", (path,)).fetchall()]
    if ids:
        qmarks = ",".join("?" * len(ids))
        cur.execute(f"DELETE FROM node_chunks WHERE chunk_id IN ({qmarks})", ids)
        cur.execute("DELETE FROM chunks WHERE path=?", (path,))
    # Typed relations are scoped to their source file, so drop them too.
    cur.execute("DELETE FROM relations WHERE path=?", (path,))
    cur.execute("DELETE FROM relations2 WHERE path=?", (path,))


def _clean_orphan_nodes(cur: sqlite3.Cursor) -> None:
    # Keep canonical targets alive even if they hold no direct mentions: an alias
    # node's canonical_id may point at them (LLM canonicalization pass).
    cur.execute(
        "DELETE FROM nodes WHERE id NOT IN (SELECT DISTINCT node_id FROM node_chunks) "
        "AND id NOT IN (SELECT canonical_id FROM nodes WHERE canonical_id IS NOT NULL)"
    )


def _bump_write_gen(cur: sqlite3.Cursor) -> None:
    """Increment a monotonic write counter so the sqlite-vec index knows when it
    is stale (chunk_ids are reassigned on replace, so a row count alone can miss
    changes). Cheap; written inside the same transaction as the data change."""
    cur.execute(
        "INSERT INTO meta(key, value) VALUES('write_gen', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
    )


def _matrix(ws_path: str) -> tuple[list[int], np.ndarray]:
    key = db_path(ws_path)
    with _cache_lock:
        cached = _VEC_CACHE.get(key)
        if cached is not None:
            return cached
    conn = _connect(ws_path)
    try:
        rows = conn.execute("SELECT id, vec FROM chunks ORDER BY id").fetchall()
    finally:
        conn.close()
    ids = [r[0] for r in rows]
    if rows:
        mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    else:
        mat = np.zeros((0, _active_dim()), dtype=np.float32)
    with _cache_lock:
        _VEC_CACHE[key] = (ids, mat)
    return ids, mat


def _vec_loadable() -> bool:
    """Whether this interpreter can load the sqlite-vec extension (probed once)."""
    global _VEC_OK
    if _VEC_OK is None:
        try:
            import sqlite_vec

            c = sqlite3.connect(":memory:")
            c.enable_load_extension(True)
            c.load_extension(sqlite_vec.loadable_path())
            c.execute("SELECT vec_version()").fetchone()
            c.close()
            _VEC_OK = True
        except Exception as e:
            log.info("sqlite-vec unavailable (%s); using numpy vector search", e)
            _VEC_OK = False
    return _VEC_OK


def _vec_search(ws_path: str, query_vec: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Indexed KNN via sqlite-vec. The ``vchunks`` vec0 table is (re)built from
    the chunk BLOBs whenever the write generation advanced, so it self-heals
    after any index change without threading sync through every write path."""
    import sqlite_vec

    conn = sqlite3.connect(db_path(ws_path))
    try:
        conn.enable_load_extension(True)
        conn.load_extension(sqlite_vec.loadable_path())
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vchunks USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, emb float[{_active_dim()}] distance_metric=cosine)"
        )
        gen_row = conn.execute("SELECT value FROM meta WHERE key='write_gen'").fetchone()
        write_gen = gen_row[0] if gen_row else "0"
        built_row = conn.execute("SELECT value FROM meta WHERE key='vec_built_gen'").fetchone()
        if (built_row[0] if built_row else None) != write_gen:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM vchunks")
            for cid, blob in conn.execute("SELECT id, vec FROM chunks").fetchall():
                # Skip non-finite vectors (a transient NaN from a past index run):
                # vec0 MATCH chokes on them, and they can't match anything anyway.
                if blob is None or not np.isfinite(np.frombuffer(blob, dtype=np.float32)).all():
                    continue
                conn.execute("INSERT INTO vchunks(chunk_id, emb) VALUES (?,?)", (cid, blob))
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('vec_built_gen', ?)", (write_gen,)
            )
            conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM vchunks").fetchone()[0]
        if n == 0:
            return []
        q = np.asarray(query_vec, dtype=np.float32).ravel().tobytes()
        rows = conn.execute(
            "SELECT chunk_id, distance FROM vchunks WHERE emb MATCH ? AND k=? ORDER BY distance",
            (q, min(k, n)),
        ).fetchall()
        return [(int(c), float(d)) for c, d in rows]
    finally:
        conn.close()


def _fetch_chunks(ws_path: str, chunk_ids: list[int]) -> list[dict]:
    if not chunk_ids:
        return []
    conn = _connect(ws_path)
    try:
        qmarks = ",".join("?" * len(chunk_ids))
        rows = conn.execute(
            f"SELECT id, path, start_line, end_line, text FROM chunks WHERE id IN ({qmarks})",
            chunk_ids,
        ).fetchall()
    finally:
        conn.close()
    return [
        {"chunk_id": r[0], "path": r[1], "start_line": r[2], "end_line": r[3], "text": r[4]}
        for r in rows
    ]
