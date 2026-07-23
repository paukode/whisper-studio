"""Entity nodes: description context/persistence and duplicate-node dedup
(lexical + semantic passes)."""

from __future__ import annotations

import logging
import re
import unicodedata

from ..config import (
    ENTITY_SEMANTIC_MERGE,
    ENTITY_SEMANTIC_THRESHOLD,
    SALIENCE_GRAPH_FLOOR,
)
from .base import (
    _GENERIC_ENTITY_NAMES,
    _SEMANTIC_MERGE_MAX,
    _bump_write_gen,
    _connect,
    _invalidate,
)

log = logging.getLogger("whisper-studio")


def entities_for_description(ws_path: str, limit: int = 400, max_ctx: int = 3) -> list[dict]:
    """Salient entities with a few sample chunk texts they appear in — the context
    an LLM needs to write a one-line description. Junk/generic nodes are skipped
    (below the graph salience floor), and the budget is spent on the highest-df
    real entities first. Returns ``[{name, label, contexts: [str]}]``."""
    conn = _connect(ws_path)
    try:
        rows = conn.execute(
            "SELECT id, name, label FROM nodes "
            "WHERE COALESCE(salience, 0.5) >= ? ORDER BY COALESCE(df, 0) DESC, id",
            (SALIENCE_GRAPH_FLOOR,),
        ).fetchall()
        out: list[dict] = []
        for nid, name, label in rows:
            if (name or "").lower() in _GENERIC_ENTITY_NAMES:
                continue
            ctx = conn.execute(
                "SELECT c.text FROM node_chunks nc JOIN chunks c ON c.id = nc.chunk_id "
                "WHERE nc.node_id=? LIMIT ?",
                (nid, max_ctx),
            ).fetchall()
            out.append({"name": name, "label": label, "contexts": [r[0] for r in ctx if r[0]]})
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def set_node_descriptions(ws_path: str, items: list[dict]) -> int:
    """Persist ``[{name, label, description}]`` onto matching nodes (capped at 500
    chars). Returns how many nodes were updated."""
    conn = _connect(ws_path)
    n = 0
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        for it in items:
            d = (it.get("description") or "").strip()
            if not d:
                continue
            cur.execute(
                "UPDATE nodes SET description=? WHERE name=? AND label=?",
                (d[:500], it.get("name"), it.get("label")),
            )
            n += cur.rowcount
        if n:
            _bump_write_gen(cur)
        conn.commit()
    finally:
        conn.close()
    if n:
        _invalidate(ws_path)
    return n


_POSSESSIVE = re.compile(r"['’]s\b")


def _norm_entity(name: str) -> str:
    """Normalised key for de-duping entity nodes that are textual variants of the
    same name: NFKC, case-folded, whitespace-collapsed, possessive and surrounding
    punctuation stripped. Deliberately conservative — it merges "John Doe" /
    "JOHN DOE" / "John  Doe" / "John Doe's", but NOT different spellings like
    "J. Doe" (no fuzzy matching, so distinct people are never collapsed)."""
    s = unicodedata.normalize("NFKC", name or "").strip().lower()
    s = _POSSESSIVE.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .,:;\"'`-")


def _merge_group(cur, members: list[tuple[int, str]]) -> int:
    """Merge a group of duplicate entity nodes into one canonical node.

    ``members`` is ``[(node_id, display_name), …]``. Canonical = the variant
    appearing in the most chunks; tie-break on the longer (usually better-cased /
    more complete) display name. Repoints node_chunks onto the canonical node and
    deletes the rest. Returns how many nodes were removed. Caller owns the
    transaction."""
    if len(members) < 2:
        return 0
    counts = {
        nid: cur.execute("SELECT COUNT(*) FROM node_chunks WHERE node_id=?", (nid,)).fetchone()[0]
        for nid, _ in members
    }
    canon_id = max(members, key=lambda m: (counts[m[0]], len(m[1])))[0]
    removed = 0
    for nid, _ in members:
        if nid == canon_id:
            continue
        cur.execute(
            "INSERT OR IGNORE INTO node_chunks(node_id, chunk_id, score) "
            "SELECT ?, chunk_id, score FROM node_chunks WHERE node_id=?",
            (canon_id, nid),
        )
        cur.execute("DELETE FROM node_chunks WHERE node_id=?", (nid,))
        # Repoint node-id-keyed relations onto the canonical node so a merge never
        # dangles a typed relation (INSERT OR IGNORE respects the unique key; then
        # drop the loser's rows and any self-loops the repoint created).
        cur.execute(
            "UPDATE OR IGNORE relations2 SET src_node_id=? WHERE src_node_id=?", (canon_id, nid)
        )
        cur.execute(
            "UPDATE OR IGNORE relations2 SET tgt_node_id=? WHERE tgt_node_id=?", (canon_id, nid)
        )
        cur.execute("DELETE FROM relations2 WHERE src_node_id=? OR tgt_node_id=?", (nid, nid))
        cur.execute("DELETE FROM relations2 WHERE src_node_id=tgt_node_id")
        cur.execute("DELETE FROM nodes WHERE id=?", (nid,))
        removed += 1
    return removed


def _uf_find(parent: list, x: int) -> int:
    """Union-find root with path-halving."""
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _embed_names(names: list[str]):
    """Embed entity names with the workspace embedder (rows L2-normalized, so a
    dot product is cosine). Returns an (n, dim) ndarray, or ``None`` if the
    embedder can't load — callers then fall back to lexical-only dedup."""
    try:
        from .. import embedder

        vecs = embedder.embed_documents(list(names))
        return vecs if getattr(vecs, "shape", (0,))[0] == len(names) else None
    except Exception as e:  # noqa: BLE001 — semantic merge is best-effort
        log.debug("Entity embedding for semantic merge unavailable: %s", e)
        return None


def _semantic_merge(cur, threshold: float) -> int:
    """Second dedup pass: merge entity nodes whose NAMES are semantically near-
    duplicate *within the same label* (embedding cosine >= ``threshold``). Catches
    variants the conservative lexical key can't — "Postgres"/"PostgreSQL",
    "GHA"/"GitHub Actions". Per-label blocking keeps it precise and bounds the
    O(n²) cosine to one label at a time; labels with more than
    ``_SEMANTIC_MERGE_MAX`` entities are skipped. Best-effort: if the embedder
    can't load, the lexical result stands. Caller owns the transaction."""
    rows = cur.execute("SELECT id, name, label FROM nodes").fetchall()
    by_label: dict = {}
    for nid, name, label in rows:
        by_label.setdefault(label, []).append((nid, name))
    merged = 0
    for members in by_label.values():
        n = len(members)
        if n < 2 or n > _SEMANTIC_MERGE_MAX:
            continue
        vecs = _embed_names([m[1] for m in members])
        if vecs is None:
            return merged  # embedder unavailable — stop; lexical result stands
        sims = vecs @ vecs.T  # rows L2-normalized -> cosine
        parent = list(range(n))
        for i in range(n):
            row = sims[i]
            for j in range(i + 1, n):
                if row[j] >= threshold:
                    ri, rj = _uf_find(parent, i), _uf_find(parent, j)
                    if ri != rj:
                        parent[max(ri, rj)] = min(ri, rj)
        comps: dict = {}
        for i in range(n):
            comps.setdefault(_uf_find(parent, i), []).append(members[i])
        for grp in comps.values():
            merged += _merge_group(cur, grp)
    return merged


def dedupe_entities(ws_path: str) -> int:
    """Collapse duplicate entity nodes into one canonical node, repointing
    node_chunks. Two passes inside one transaction:

      1. Lexical — names that normalise equal within a label (NFKC/case/punct).
      2. Semantic — names whose embeddings are near-duplicate within a label
         (cosine >= ENTITY_SEMANTIC_THRESHOLD), catching spelling/abbreviation
         variants the lexical key misses. Skipped when ENTITY_SEMANTIC_MERGE is
         off or the embedder can't load.

    Returns the number of nodes merged away. Safe to run after every build."""
    conn = _connect(ws_path)
    merged = 0
    try:
        cur = conn.cursor()
        rows = cur.execute("SELECT id, name, label FROM nodes").fetchall()
        groups: dict[tuple, list[tuple[int, str]]] = {}
        for nid, name, label in rows:
            groups.setdefault((label, _norm_entity(name)), []).append((nid, name))
        cur.execute("BEGIN")
        for members in groups.values():
            merged += _merge_group(cur, members)
        if ENTITY_SEMANTIC_MERGE:
            merged += _semantic_merge(cur, ENTITY_SEMANTIC_THRESHOLD)
        if merged:
            _bump_write_gen(cur)
        conn.commit()
    finally:
        conn.close()
    if merged:
        _invalidate(ws_path)
    return merged
