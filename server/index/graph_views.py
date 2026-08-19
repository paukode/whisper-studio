"""Derived file-relationship graph over a workspace index.

A *recomputable* view built on read from the same SQLite store: files as nodes,
shared-entity edges, Leiden communities. The explorer's overview consumes it
for topic cards and connection ranking (explore_views); the public entry point
stays on ``store`` as a thin cached wrapper (``store.file_graph``).

This module imports ``store`` for its connection helper and tuning constants and
references them lazily (at call time), so the store<->graph_views import is not a
cycle in practice.
"""

from __future__ import annotations

import logging
import os

from server.index import store
from server.index.config import SALIENCE_GRAPH_FLOOR

log = logging.getLogger("whisper-studio")


def _detect_communities(edges: list) -> dict:
    """Cluster the file nodes by the weighted co-occurrence edges and return
    ``{node_id: community_index}``. Uses Leiden (leidenalg + igraph) — which,
    unlike Louvain, guarantees well-connected communities — and falls back to
    networkx Louvain, then to a single community, so the overview always gets
    groups even if the optional deps are missing. Deterministic (fixed seed).
    Only nodes that appear in an edge are clustered; isolated nodes are omitted
    (the caller marks them -1)."""
    present: list = []
    seen: set = set()
    for e in edges:
        for v in (e["source"], e["target"]):
            if v not in seen:
                seen.add(v)
                present.append(v)
    if not present:
        return {}
    idx = {v: i for i, v in enumerate(present)}
    weights = [max(float(e.get("weight_norm") or e.get("weight") or 1.0), 1e-6) for e in edges]
    try:
        import igraph as ig
        import leidenalg

        g = ig.Graph(n=len(present), edges=[(idx[e["source"]], idx[e["target"]]) for e in edges])
        g.es["weight"] = weights
        part = leidenalg.find_partition(
            g, leidenalg.ModularityVertexPartition, weights="weight", seed=42
        )
        return {present[vi]: ci for ci, members in enumerate(part) for vi in members}
    except Exception as e:  # noqa: BLE001 — optional dep / any failure -> fallback
        log.debug("Leiden unavailable (%s); falling back to networkx Louvain", e)
    try:
        import networkx as nx

        g = nx.Graph()
        g.add_nodes_from(present)
        for e, w in zip(edges, weights, strict=False):
            g.add_edge(e["source"], e["target"], weight=w)
        parts = nx.community.louvain_communities(g, weight="weight", seed=42)
        return {v: ci for ci, members in enumerate(parts) for v in members}
    except Exception as e:  # noqa: BLE001
        log.debug("Community detection failed (%s); using a single community", e)
        return {v: 0 for v in present}


def _annotate_communities(nodes: list, edges: list) -> None:
    """Mutate ``nodes`` in place, adding a ``community`` index (Leiden over the
    weighted edges; -1 for isolated nodes) and an edge ``degree``. These power
    the overview's topic cards and most-connected ranking."""
    comm = _detect_communities(edges)
    deg: dict = {}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    for n in nodes:
        nid = n.get("id")
        n["community"] = comm.get(nid, -1)
        n["degree"] = deg.get(nid, 0)


def file_graph_impl(ws_path: str, max_edges: int = 300) -> dict:
    """File-relationship graph: nodes are indexed files, edges connect two files
    that share extracted entities (weight = number of shared entities). Edges are
    derived on the fly and capped to the strongest ``max_edges`` so the view stays
    cheap on large repos. Nodes are annotated with a Leiden ``community`` index
    and edge ``degree``."""
    generic = sorted(store._GENERIC_ENTITY_NAMES)
    gph = ",".join("?" for _ in generic)
    conn = store._connect(ws_path)
    try:
        nodes = [
            {"id": p, "name": os.path.basename(p), "chunks": n, "type": "file"}
            for p, n in conn.execute(
                "SELECT path, n_chunks FROM files WHERE n_chunks > 0"
            ).fetchall()
        ]
        # The ``good`` CTE drops low-signal entities before the self-join: junk
        # (below the salience floor — the corpus-adaptive filter that also catches
        # Polish generics a static list can't), ultra-common boilerplate (in >
        # _MAX_NODE_FANOUT chunks, kept as an absolute bound), and the generic
        # category words (a NULL-salience fallback for un-migrated DBs). ``deg`` is
        # each file's distinct good-entity count, used to PMI-normalize the edge so
        # a pair sharing entities merely because both files are entity-dense is
        # down-weighted. Edges are ranked by the normalized score, not raw count.
        rows = conn.execute(
            f"""WITH freq AS (
                   SELECT node_id, COUNT(*) AS cnt FROM node_chunks GROUP BY node_id
               ),
               good AS (
                   SELECT nc.node_id, nc.chunk_id FROM node_chunks nc
                   JOIN freq f ON f.node_id = nc.node_id
                   JOIN nodes nn ON nn.id = nc.node_id
                   WHERE f.cnt <= ? AND COALESCE(nn.salience, 0.5) >= ?
                     AND LOWER(nn.name) NOT IN ({gph})
               ),
               deg AS (
                   SELECT c.path AS p, COUNT(DISTINCT g.node_id) AS d
                   FROM good g JOIN chunks c ON c.id = g.chunk_id
                   GROUP BY c.path
               )
               SELECT c1.path AS a, c2.path AS b,
                      COUNT(DISTINCT g1.node_id) AS w,
                      GROUP_CONCAT(DISTINCT nd.name) AS ents,
                      dga.d AS deg_a, dgb.d AS deg_b
               FROM good g1
               JOIN good g2 ON g1.node_id = g2.node_id
               JOIN chunks c1 ON c1.id = g1.chunk_id
               JOIN chunks c2 ON c2.id = g2.chunk_id
               JOIN nodes nd ON nd.id = g1.node_id
               JOIN deg dga ON dga.p = c1.path
               JOIN deg dgb ON dgb.p = c2.path
               WHERE c1.path < c2.path
               GROUP BY c1.path, c2.path
               ORDER BY (CAST(w AS REAL) * w) / (deg_a * deg_b) DESC, w DESC
               LIMIT ?""",
            [store._MAX_NODE_FANOUT, SALIENCE_GRAPH_FLOOR, *generic, max_edges + 1],
        ).fetchall()
    finally:
        conn.close()
    truncated = len(rows) > max_edges
    edges = []
    for a, b, w, ents, deg_a, deg_b in rows[:max_edges]:
        # GROUP_CONCAT joins with ',' — entity names are short noun phrases, so a
        # rare embedded comma only over-splits the display sample, never the weight.
        names = [s for s in (ents or "").split(",") if s][: store._EDGE_ENTITY_SAMPLE]
        # Cosine-style overlap in [0,1]: shared / sqrt(deg_a*deg_b). Raw count is
        # kept as ``weight`` for display; ``weight_norm`` drives ranking.
        norm = w / ((deg_a * deg_b) ** 0.5) if deg_a and deg_b else 0.0
        edges.append(
            {
                "source": a,
                "target": b,
                "weight": w,
                "weight_norm": round(norm, 4),
                "entities": names,
            }
        )
    _annotate_communities(nodes, edges)
    return {"nodes": nodes, "edges": edges, "truncated": truncated}
