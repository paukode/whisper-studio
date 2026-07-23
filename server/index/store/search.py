"""Retrieval: dense vector search (cached), the GraphRAG entity-overlap hop,
per-file chunk fetch, and FTS5 keyword search."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import sqlite3

import numpy as np

from ..config import FTS_SCHEMA_VER, PL_STOPWORDS, SALIENCE_JUNK_FLOOR
from .base import (
    _GENERIC_ENTITY_NAMES,
    _MAX_NODE_FANOUT,
    _cached_derived,
    _connect,
    _fetch_chunks,
    _matrix,
    _vec_loadable,
    _vec_search,
)

log = logging.getLogger("whisper-studio")


def search(ws_path: str, query_vec: np.ndarray, k: int = 8) -> list[dict]:
    """Cached wrapper for vector search: memoizes results by query bytes + k,
    invalidated on any index write. See ``_search_impl`` for the ranking."""
    qh = hashlib.sha1(query_vec.tobytes()).hexdigest()[:16]
    return _cached_derived(
        ws_path, ("search", qh, int(k)), lambda: _search_impl(ws_path, query_vec, k)
    )


def _search_impl(ws_path: str, query_vec: np.ndarray, k: int = 8) -> list[dict]:
    """Top-k chunks by cosine similarity to ``query_vec`` (already normalized).

    Uses sqlite-vec (indexed KNN) when the extension can load, else a brute-force
    numpy matrix — same ranking, since both compare L2-normalized vectors."""
    if _vec_loadable():
        try:
            hits = _vec_search(ws_path, query_vec, k)  # [(chunk_id, cosine_distance)]
            by_id = {r["chunk_id"]: r for r in _fetch_chunks(ws_path, [c for c, _ in hits])}
            out = []
            for cid, dist in hits:
                r = by_id.get(cid)
                if r:
                    r = dict(r)
                    r["score"] = 1.0 - dist  # cosine distance → similarity
                    out.append(r)
            return out
        except Exception as e:
            log.warning("sqlite-vec search failed (%s); falling back to numpy", e)
    ids, mat = _matrix(ws_path)
    if mat.shape[0] == 0:
        return []
    q = np.asarray(query_vec, dtype=np.float32).ravel()
    scores = mat @ q
    # A non-finite stored vector (transient NaN from a past run) yields a NaN
    # score; push those to -inf so they never rank and never poison argsort.
    scores[~np.isfinite(scores)] = -np.inf
    k = min(k, len(ids))
    top = np.argsort(-scores)[:k]
    rows = _fetch_chunks(ws_path, [ids[i] for i in top])
    by_id = {r["chunk_id"]: r for r in rows}
    out = []
    for i in top:
        if not np.isfinite(scores[i]):
            continue
        r = by_id.get(ids[i])
        if r:
            r = dict(r)
            r["score"] = float(scores[i])
            out.append(r)
    return out


def expand(ws_path: str, chunk_ids: list[int], limit: int = 4) -> list[dict]:
    """GraphRAG hop: chunks that share entities with ``chunk_ids`` (excluding them).

    Ranked by salience- and IDF-weighted shared-entity mass, not raw count, so a
    rare, discriminative entity outweighs a ubiquitous one. Junk entities (below
    the salience floor, generic names, or boilerplate hubs above _MAX_NODE_FANOUT)
    are excluded so a word appearing in hundreds of chunks can't flood the hop.
    Robust to un-migrated DBs: salience defaults to 0.5 and df is computed live."""
    if not chunk_ids:
        return []
    conn = _connect(ws_path)
    try:
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 1
        qmarks = ",".join("?" * len(chunk_ids))
        # Seed nodes with the numbers needed to weight them: name (generic filter),
        # salience (junk floor), and live chunk fanout (IDF + boilerplate cap).
        seed = conn.execute(
            f"""SELECT n.id, n.name, COALESCE(n.salience, 0.5) AS sal,
                       (SELECT COUNT(*) FROM node_chunks x WHERE x.node_id = n.id) AS df
                FROM nodes n
                WHERE n.id IN (
                    SELECT DISTINCT node_id FROM node_chunks WHERE chunk_id IN ({qmarks})
                )""",
            chunk_ids,
        ).fetchall()
        weights: dict[int, float] = {}
        for nid, name, sal, df in seed:
            if sal < SALIENCE_JUNK_FLOOR:
                continue
            if (name or "").lower() in _GENERIC_ENTITY_NAMES:
                continue
            if df > _MAX_NODE_FANOUT:  # boilerplate hub — not a relatedness signal
                continue
            weights[nid] = sal * math.log(1 + n_chunks / max(df, 1))
        if not weights:
            return []
        good = list(weights)
        gmarks = ",".join("?" * len(good))
        rows = conn.execute(
            f"""SELECT chunk_id, node_id FROM node_chunks
                WHERE node_id IN ({gmarks}) AND chunk_id NOT IN ({qmarks})""",
            good + chunk_ids,
        ).fetchall()
    finally:
        conn.close()
    scored: dict[int, dict] = {}
    for cid, nid in rows:
        agg = scored.setdefault(cid, {"score": 0.0, "shared": 0})
        agg["score"] += weights.get(nid, 0.0)
        agg["shared"] += 1
    ranked = sorted(scored.items(), key=lambda kv: (kv[1]["score"], kv[1]["shared"]), reverse=True)[
        :limit
    ]
    chunks = {r["chunk_id"]: r for r in _fetch_chunks(ws_path, [cid for cid, _ in ranked])}
    out = []
    for cid, agg in ranked:
        r = chunks.get(cid)
        if r:
            r = dict(r)
            r["shared_entities"] = agg["shared"]
            r["graph_score"] = round(agg["score"], 4)
            out.append(r)
    return out


def chunks_for_file(ws_path: str, rel_path: str) -> list[dict]:
    """Every chunk of one file, in document (line) order. Used by grounding's
    auto-merge to pull a small, relevant document in full (e.g. a CV's whole
    employment history) once one of its chunks ranks high."""
    conn = _connect(ws_path)
    try:
        rows = conn.execute(
            "SELECT id, path, start_line, end_line, text FROM chunks "
            "WHERE path=? ORDER BY start_line",
            (rel_path,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"chunk_id": r[0], "path": r[1], "start_line": r[2], "end_line": r[3], "text": r[4]}
        for r in rows
    ]


# Common English stopwords dropped from keyword queries so a chunk that merely
# shares "the"/"is"/"how" with the question isn't treated as a relevant keyword
# hit. Content terms (names, ids, filenames like "buddy") are what we want.
_FTS_STOPWORDS = frozenset(
    (
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "she",
        "should",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    )
)

# English + Polish stopwords for the keyword leg (the corpus is bilingual).
_ALL_STOPWORDS = _FTS_STOPWORDS | PL_STOPWORDS

# Suffix inflections stripped to form a prefix-query stem so "umowy"/"umowie"/
# "umowa" co-match on "umow*" (and EN "meeting"/"meetings" on "meeting*"). Longest
# suffixes first; a stem must keep at least 4 chars. PL then EN.
_STEM_SUFFIXES = (
    "owie",
    "ami",
    "ach",
    "iem",
    "ów",
    "om",
    "ie",
    "em",
    "ą",
    "ę",
    "y",
    "i",
    "a",
    "ing",
    "ed",
    "es",
    "s",
)


def _term_expr(t: str) -> str:
    """FTS5 sub-expression for one query term: the exact term, plus a prefix query
    on its stem when a common inflectional suffix can be stripped."""
    if len(t) >= 5:
        for suf in _STEM_SUFFIXES:
            if t.endswith(suf) and len(t) - len(suf) >= 4:
                return f'("{t}" OR "{t[: -len(suf)]}"*)'
    return f'"{t}"'


def fts_search(ws_path: str, query_text: str, k: int = 8) -> list[dict]:
    """Keyword/BM25 search via SQLite FTS5 over chunk text + filename path. The
    ``fts_chunks`` table is (re)built from the chunks whenever the write
    generation advanced — same self-healing pattern as the vec index — so it
    needs no separate sync and no re-index to come online. Returns chunk dicts
    (each with a ``_bm25`` relevance score, more-negative = better) in BM25 rank
    order; empty if FTS5 is unavailable or the query has no content terms.

    Indexing the path means a filename token (e.g. "buddy" in buddy.json) is
    matchable even when the content never mentions it. Stopwords are stripped so
    only content terms drive a match. Tokenization is FTS5's default (unicode61),
    which handles Latin/Cyrillic but not space-free scripts (CJK/Thai) — those
    rely on the dense vector leg."""
    terms = [
        t
        for t in re.findall(r"\w+", (query_text or "").lower())
        if len(t) >= 2 and t not in _ALL_STOPWORDS
    ][:24]
    if not terms:
        return []
    match_expr = " OR ".join(_term_expr(t) for t in terms)
    conn = _connect(ws_path)
    try:
        # Rebuild the FTS table when its tokenizer version changed (v2 folds
        # diacritics for Polish). Clearing fts_built_gen makes the self-healing
        # rebuild below repopulate it.
        try:
            ver_row = conn.execute("SELECT value FROM meta WHERE key='fts_schema_ver'").fetchone()
            if (ver_row[0] if ver_row else None) != str(FTS_SCHEMA_VER):
                conn.execute("DROP TABLE IF EXISTS fts_chunks")
                conn.execute("DELETE FROM meta WHERE key='fts_built_gen'")
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('fts_schema_ver', ?)",
                    (str(FTS_SCHEMA_VER),),
                )
                conn.commit()
        except sqlite3.OperationalError:
            pass  # locked mid-migration; a later query retries
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks "
                'USING fts5(chunk_id UNINDEXED, text, path, tokenize="unicode61 remove_diacritics 2")'
            )
        except sqlite3.OperationalError as e:
            log.info("FTS5 unavailable (%s); keyword search disabled", e)
            return []
        # BEGIN IMMEDIATE takes the write lock up front, so the write_gen read
        # below can't race a concurrent build into stamping a wrong fts_built_gen.
        built_row = conn.execute("SELECT value FROM meta WHERE key='fts_built_gen'").fetchone()
        gen_row = conn.execute("SELECT value FROM meta WHERE key='write_gen'").fetchone()
        if (built_row[0] if built_row else None) != (gen_row[0] if gen_row else "0"):
            conn.execute("BEGIN IMMEDIATE")
            write_gen = (
                conn.execute("SELECT value FROM meta WHERE key='write_gen'").fetchone() or ["0"]
            )[0]
            conn.execute("DELETE FROM fts_chunks")
            conn.executemany(
                "INSERT INTO fts_chunks(chunk_id, text, path) VALUES (?,?,?)",
                conn.execute("SELECT id, text, path FROM chunks").fetchall(),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('fts_built_gen', ?)", (write_gen,)
            )
            conn.commit()
        rows = conn.execute(
            "SELECT chunk_id, bm25(fts_chunks) AS s FROM fts_chunks "
            "WHERE fts_chunks MATCH ? ORDER BY s LIMIT ?",
            (match_expr, k),
        ).fetchall()
    except sqlite3.OperationalError as e:  # malformed MATCH, locked db, etc.
        log.warning("FTS keyword query failed (%s)", e)
        return []
    finally:
        conn.close()
    if not rows:
        return []
    score = {cid: s for cid, s in rows}
    order = {cid: i for i, (cid, _) in enumerate(rows)}
    chunks = _fetch_chunks(ws_path, [cid for cid, _ in rows])
    for c in chunks:
        c["_bm25"] = score.get(c["chunk_id"], 0.0)
    chunks.sort(key=lambda c: order.get(c["chunk_id"], 1 << 30))
    return chunks
