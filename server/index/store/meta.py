"""Index metadata + lifecycle: meta key/value, existence/stats probes, on-disk
index removal, and enumeration of all indexed workspaces."""

from __future__ import annotations

import json
import os
import sqlite3

from .. import paths
from ..config import EMBED_MODEL
from ..paths import db_path, workspace_index_dir
from .base import _connect, _invalidate


def set_meta(ws_path: str, **kv) -> None:
    conn = _connect(ws_path)
    try:
        for k, v in kv.items():
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (k, json.dumps(v)))
        conn.commit()
    finally:
        conn.close()


def get_meta(ws_path: str) -> dict:
    conn = _connect(ws_path)
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    finally:
        conn.close()
    return {k: json.loads(v) for k, v in rows}


def has_index(ws_path: str, embed_backend: str | None = None) -> bool:
    """Whether an index DB exists for the active (or given) embed backend, WITHOUT
    creating one. Read paths MUST gate on this before calling _connect-backed
    helpers (stats / graph views): _connect() unconditionally creates the db file
    (and workspace dir) on open, so an unguarded read of a folder that was indexed
    under the OTHER embed backend (indexes are per-backend — index.db for qwen3,
    index-<backend>.db for the rest) fabricates an empty active-backend index and
    silently reports it as empty."""
    return os.path.exists(db_path(ws_path, embed_backend))


def stats(ws_path: str) -> dict:
    # No index for the active backend: report empty WITHOUT touching disk. Opening
    # a missing db path would create an empty file (see has_index), fabricating a
    # zeroed index for a folder indexed under a different embed backend.
    if not has_index(ws_path):
        return {
            "files": 0,
            "chunks": 0,
            "nodes": 0,
            "last_indexed_at": None,
            "embed_model": EMBED_MODEL,
        }
    conn = _connect(ws_path)
    try:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        conn.close()
    meta = get_meta(ws_path)
    return {
        "files": files,
        "chunks": chunks,
        "nodes": nodes,
        "last_indexed_at": meta.get("last_indexed_at"),
        "embed_model": meta.get("embed_model", EMBED_MODEL),
    }


def remove_index(ws_path: str) -> None:
    """Delete the entire on-disk index for a workspace."""
    import shutil

    _invalidate(ws_path)
    d = workspace_index_dir(ws_path)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


def list_indexed_workspaces() -> list[str]:
    """Absolute workspace paths that currently have an index on disk.

    A folder can hold indexes for more than one embed backend, each in its own
    db file: ``index.db`` for qwen3 (local) and ``index-<backend>.db`` for the
    others (e.g. ``index-cohere.db`` in cloud mode). Recognize any of them so an
    index built in a non-default mode is not invisible — a hardcoded ``index.db``
    lookup hid every cloud-mode index, emptying the search-index picker and the
    chat's grounding default. Reads the ``workspace`` value each index db recorded
    at build time, so the scheduler can refresh every indexed folder, not just the
    connected one."""
    base = paths.INDEX_DATA_DIR
    if not os.path.isdir(base):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for name in sorted(os.listdir(base)):
        wdir = os.path.join(base, name)
        if not os.path.isdir(wdir):
            continue
        db_files = sorted(
            f
            for f in os.listdir(wdir)
            if f == "index.db" or (f.startswith("index-") and f.endswith(".db"))
        )
        for db in db_files:
            conn = sqlite3.connect(os.path.join(wdir, db))
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='workspace'").fetchone()
            except Exception:
                row = None
            finally:
                conn.close()
            if row:
                ws = json.loads(row[0])
                if ws not in seen:
                    seen.add(ws)
                    out.append(ws)
                # One workspace path per dir; stop at the first db that has it.
                break
    return out
