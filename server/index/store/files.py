"""Per-file index writes: manifest, replace, touch, delete, typed relations."""

from __future__ import annotations

import numpy as np

from .base import (
    _bump_write_gen,
    _clean_orphan_nodes,
    _connect,
    _delete_file_rows,
    _invalidate,
)


def get_manifest(ws_path: str) -> dict[str, dict]:
    """``{path: {hash, size, mtime}}`` for change detection."""
    conn = _connect(ws_path)
    try:
        rows = conn.execute("SELECT path, hash, size, mtime FROM files").fetchall()
    finally:
        conn.close()
    return {r[0]: {"hash": r[1], "size": r[2], "mtime": r[3]} for r in rows}


def replace_file(ws_path: str, path: str, file_meta: dict, chunk_records: list[dict]) -> None:
    """Atomically replace all index data for one file.

    ``chunk_records`` = ``[{start_line, end_line, text, vec(np.ndarray), entities:[{name,label}]}]``.
    Deletes the file's old chunks/links first, then inserts the new ones, upserts
    entity nodes, and links node↔chunk. Orphan nodes are cleaned up.
    """
    conn = _connect(ws_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        _delete_file_rows(cur, path)
        cur.execute(
            "INSERT OR REPLACE INTO files(path, hash, size, mtime, n_chunks) VALUES (?,?,?,?,?)",
            (path, file_meta["hash"], file_meta["size"], file_meta["mtime"], len(chunk_records)),
        )
        for rec in chunk_records:
            vec = np.asarray(rec["vec"], dtype=np.float32).ravel()
            cur.execute(
                "INSERT INTO chunks(path, start_line, end_line, text, vec) VALUES (?,?,?,?,?)",
                (path, rec["start_line"], rec["end_line"], rec["text"], vec.tobytes()),
            )
            chunk_id = cur.lastrowid
            for ent in rec.get("entities", []):
                cur.execute(
                    "INSERT OR IGNORE INTO nodes(name, label) VALUES (?,?)",
                    (ent["name"], ent["label"]),
                )
                node_id = cur.execute(
                    "SELECT id FROM nodes WHERE name=? AND label=?", (ent["name"], ent["label"])
                ).fetchone()[0]
                cur.execute(
                    "INSERT OR IGNORE INTO node_chunks(node_id, chunk_id, score) VALUES (?,?,?)",
                    (node_id, chunk_id, ent.get("score")),
                )
        _clean_orphan_nodes(cur)
        _bump_write_gen(cur)
        conn.commit()
    finally:
        conn.close()
    _invalidate(ws_path)


def touch_file(ws_path: str, path: str, size: int, mtime: float) -> None:
    """Refresh a file's size/mtime gate when its content hash is unchanged, so the
    next run skips it without re-hashing. No chunk/vector changes."""
    conn = _connect(ws_path)
    try:
        conn.execute("UPDATE files SET size=?, mtime=? WHERE path=?", (size, mtime, path))
        conn.commit()
    finally:
        conn.close()


def delete_file(ws_path: str, path: str) -> None:
    conn = _connect(ws_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        _delete_file_rows(cur, path)
        cur.execute("DELETE FROM files WHERE path=?", (path,))
        _clean_orphan_nodes(cur)
        _bump_write_gen(cur)
        conn.commit()
    finally:
        conn.close()
    _invalidate(ws_path)


def set_file_relations(ws_path: str, path: str, rels: list) -> None:
    """Replace the typed relations extracted from one file. ``rels`` is a list of
    ``(source, target, type)`` or ``(source, target, type, score)`` entity-name
    tuples — ``score`` is the LLM's 1–5 confidence/strength (default 3.0).
    Idempotent per file."""
    conn = _connect(ws_path)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute("DELETE FROM relations WHERE path=?", (path,))
        for r in rels:
            s, t, ty = r[0], r[1], r[2]
            try:
                score = float(r[3]) if len(r) > 3 and r[3] is not None else 3.0
            except (TypeError, ValueError):
                score = 3.0
            if not (s and t and ty) or s == t:
                continue
            cur.execute(
                "INSERT OR IGNORE INTO relations(source, target, type, path, score) "
                "VALUES (?,?,?,?,?)",
                (s, t, ty, path, score),
            )
        conn.commit()
    finally:
        conn.close()
