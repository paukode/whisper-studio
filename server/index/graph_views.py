"""Derived graph + semantic-map views over a workspace index.

These are *recomputable* views built on read from the same SQLite store: the
file-relationship graph (entity co-occurrence edges, Leiden communities), the
entity-pivot graph ("everything about this person"), the cross-workspace graph,
and the UMAP/PCA 2D semantic-map layout. They are split out of ``store`` to keep
the core store (CRUD + vector/keyword search) under the file-size budget; the
public entry points stay on ``store`` as thin cached wrappers, so callers keep
using ``store.file_graph`` / ``store.entity_graph`` / ``store.umap_graph`` /
``store.all_workspaces_graph`` unchanged.

This module imports ``store`` for its connection helper and tuning constants and
references them lazily (at call time), so the store<->graph_views import is not a
cycle in practice.
"""

from __future__ import annotations

import logging
import os
import sqlite3

import numpy as np

from server.index import store
from server.index.config import SALIENCE_GRAPH_FLOOR

log = logging.getLogger("whisper-studio")


def _detect_communities(edges: list) -> dict:
    """Cluster the file nodes by the weighted co-occurrence edges and return
    ``{node_id: community_index}``. Uses Leiden (leidenalg + igraph) — which,
    unlike Louvain, guarantees well-connected communities — and falls back to
    networkx Louvain, then to a single community, so the graph view always gets
    coloring even if the optional deps are missing. Deterministic (fixed seed).
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
    weighted edges; -1 for isolated nodes) and an edge ``degree`` for node sizing.
    These power the community-colored graph view."""
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
    readable on large repos. Nodes are annotated with a Leiden ``community`` index
    and edge ``degree`` for the community-colored view."""
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
        # kept as ``weight`` for display; ``weight_norm`` drives ranking/thickness.
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


def entity_graph_impl(ws_path: str, name: str, label: str = "", max_files: int = 200) -> dict:
    """Entity-centric view: one entity node at the centre, linked to every file
    that mentions it (edge weight = chunks in that file that mention it). This is
    the "everything about this person" view.

    Matched by name case-insensitively so a click that only carries the display
    name still resolves; an optional ``label`` narrows it when known.
    """
    name = (name or "").strip()
    if not name:
        return {"nodes": [], "edges": [], "truncated": False}
    conn = store._connect(ws_path)
    try:
        params: list = [name.lower()]
        label_clause = ""
        if label:
            label_clause = " AND n.label = ?"
            params.append(label)
        rows = conn.execute(
            f"""SELECT c.path AS p, COUNT(DISTINCT nc.chunk_id) AS w
                FROM nodes n
                JOIN node_chunks nc ON nc.node_id = n.id
                JOIN chunks c ON c.id = nc.chunk_id
                WHERE LOWER(n.name) = ?{label_clause}
                GROUP BY c.path ORDER BY w DESC LIMIT ?""",
            params + [max_files],
        ).fetchall()
        # Canonical display name + label: the most common stored (name,label)
        # whose name case-folds to the query (a click only carries the name, and
        # casing may vary across files), so the centre shows the real spelling.
        nr = conn.execute(
            "SELECT name, label, description, COUNT(*) c FROM nodes WHERE LOWER(name)=? "
            "GROUP BY name, label ORDER BY c DESC LIMIT 1",
            (name.lower(),),
        ).fetchone()
        disp = nr[0] if nr else name
        lab = label or (nr[1] if nr else "")
        desc = (nr[2] if nr else None) or ""
        # Typed relations touching this entity (empty unless that feature ran).
        # Prefer relations2 — the node-id-keyed table that entity dedup repoints,
        # joined to ``nodes`` for the display names — so the pivot view matches the
        # deduped graph. The legacy name-keyed ``relations`` table can desync from
        # dedup (its rows are not repointed), so it is used ONLY as a fallback for
        # pre-migration DBs where relations2 is absent or entirely empty. The check
        # is "is relations2 populated at all", not "for this entity": an entity with
        # no relations2 rows genuinely has none, and must NOT resurface stale legacy
        # rows. MAX(strength) collapses the same relation seen across files.
        try:
            has_rel2 = conn.execute("SELECT 1 FROM relations2 LIMIT 1").fetchone() is not None
        except sqlite3.OperationalError:
            has_rel2 = False  # pre-migration DB without the relations2 table
        if has_rel2:
            rel_rows = conn.execute(
                "SELECT sn.name, tn.name, r.predicate, MAX(r.strength) "
                "FROM relations2 r "
                "JOIN nodes sn ON sn.id = r.src_node_id "
                "JOIN nodes tn ON tn.id = r.tgt_node_id "
                "WHERE LOWER(sn.name)=? OR LOWER(tn.name)=? "
                "GROUP BY sn.name, tn.name, r.predicate",
                (name.lower(), name.lower()),
            ).fetchall()
        else:
            rel_rows = conn.execute(
                "SELECT source, target, type, MAX(score) FROM relations "
                "WHERE LOWER(source)=? OR LOWER(target)=? "
                "GROUP BY source, target, type",
                (name.lower(), name.lower()),
            ).fetchall()
    finally:
        conn.close()
    ent_id = f"entity::{disp}"
    nodes: list[dict] = [
        {"id": ent_id, "name": disp, "label": lab, "type": "entity", "description": desc}
    ]
    edges: list[dict] = []
    for p, w in rows:
        nodes.append({"id": p, "name": os.path.basename(p), "chunks": w, "type": "file"})
        edges.append({"source": ent_id, "target": p, "weight": w})
    # Typed entity↔entity relations: add neighbour entities as nodes and a
    # labelled edge per relation (direction as extracted).
    name_l = name.lower()
    seen_ent = {name_l}
    for s, t, ty, sc in rel_rows:
        if s.lower() == name_l:
            other, src_id, tgt_id = t, ent_id, f"entity::{t}"
        else:
            other, src_id, tgt_id = s, f"entity::{s}", ent_id
        if other and other.lower() not in seen_ent:
            seen_ent.add(other.lower())
            nodes.append({"id": f"entity::{other}", "name": other, "type": "entity"})
        edges.append(
            {
                "source": src_id,
                "target": tgt_id,
                "relation": ty,
                "score": round(float(sc), 1) if sc is not None else None,
            }
        )
    return {"nodes": nodes, "edges": edges, "truncated": len(rows) >= max_files}


def all_workspaces_graph(max_edges: int = 400) -> dict:
    """Unified file-relationship graph across every indexed workspace.

    Nodes are files (``id`` = absolute path, ``group`` = source-workspace index
    for colouring). Edges link files — within OR across workspaces — that share
    an entity name, weighted by how many entities they share, with the common
    entities sampled for display and a ``cross`` flag when the two files live in
    different workspaces. Built by merging each workspace's own db on read (no
    global store), matching entities by normalised name. Boilerplate entities
    (in more than ``_MAX_FILE_FANOUT`` files corpus-wide) are dropped so the
    view stays legible."""
    from itertools import combinations

    node_meta: dict[str, dict] = {}  # abs_path -> node
    ws_legend: list[dict] = []
    ent_files: dict[str, set[str]] = {}  # normalised entity -> abs file paths
    ent_name: dict[str, str] = {}  # normalised entity -> display name

    for gi, ws in enumerate(sorted(store.list_indexed_workspaces())):
        # Only merge folders indexed under the ACTIVE embed backend; _connect would
        # otherwise fabricate an empty active-backend db for a folder indexed under
        # the other embedder (see store.has_index).
        if not store.has_index(ws):
            continue
        root = os.path.normpath(os.path.abspath(os.path.expanduser(ws)))
        try:
            conn = store._connect(ws)
        except Exception:
            continue
        try:
            files = conn.execute("SELECT path, n_chunks FROM files WHERE n_chunks > 0").fetchall()
            for rel, n in files:
                ap = os.path.join(root, rel)
                node_meta[ap] = {
                    "id": ap,
                    "name": os.path.basename(rel) or rel,
                    "chunks": n,
                    "workspace": root,
                    "group": gi,
                    "type": "file",
                }
            rows = conn.execute(
                """SELECT n.name AS nm, c.path AS p,
                          MAX(COALESCE(n.salience, 0.5)) AS sal
                   FROM nodes n
                   JOIN node_chunks nc ON nc.node_id = n.id
                   JOIN chunks c ON c.id = nc.chunk_id
                   GROUP BY n.name, c.path"""
            ).fetchall()
        except Exception:
            continue
        finally:
            conn.close()
        ws_legend.append(
            {"path": root, "name": os.path.basename(root) or root, "files": len(files), "group": gi}
        )
        for nm, rel, sal in rows:
            key = (nm or "").strip().lower()
            ap = os.path.join(root, rel)
            if not key or ap not in node_meta or sal < SALIENCE_GRAPH_FLOOR:
                continue
            ent_files.setdefault(key, set()).add(ap)
            ent_name.setdefault(key, nm)

    # Drop generic category words, then compute each file's distinct-entity
    # degree over the SAME bounded, non-generic universe used for edges so the
    # normalized weight down-weights pairs linked merely because both files are
    # entity-dense.
    deg: dict[str, int] = {}
    contrib: list[tuple[list, str]] = []
    for key, files in ent_files.items():
        if key in store._GENERIC_ENTITY_NAMES:
            continue
        if not (2 <= len(files) <= store._MAX_FILE_FANOUT):
            continue
        fs = sorted(files)
        for f in fs:
            deg[f] = deg.get(f, 0) + 1
        contrib.append((fs, ent_name[key]))

    edge_w: dict[tuple, int] = {}
    edge_ents: dict[tuple, set] = {}
    for fs, nm in contrib:
        for a, b in combinations(fs, 2):
            edge_w[(a, b)] = edge_w.get((a, b), 0) + 1
            s = edge_ents.setdefault((a, b), set())
            if len(s) < store._EDGE_ENTITY_SAMPLE:
                s.add(nm)

    def _norm(pair: tuple, w: int) -> float:
        d = deg.get(pair[0], 1) * deg.get(pair[1], 1)
        return w / (d**0.5) if d else 0.0

    ranked = sorted(edge_w.items(), key=lambda kv: _norm(kv[0], kv[1]), reverse=True)
    truncated = len(ranked) > max_edges
    edges = [
        {
            "source": a,
            "target": b,
            "weight": w,
            "weight_norm": round(_norm((a, b), w), 4),
            "entities": sorted(edge_ents.get((a, b), [])),
            "cross": node_meta[a]["workspace"] != node_meta[b]["workspace"],
        }
        for (a, b), w in ranked[:max_edges]
    ]
    nodes_list = list(node_meta.values())
    _annotate_communities(nodes_list, edges)
    return {
        "nodes": nodes_list,
        "edges": edges,
        "workspaces": ws_legend,
        "truncated": truncated,
        "root": "",
    }


def _file_mean_vectors(conn, paths: set) -> dict:
    """Mean (L2-normalized) chunk vector per file — one semantic vector per file
    for the embedding projection. Only files in ``paths`` are returned."""
    by_path: dict = {}
    for p, blob in conn.execute("SELECT path, vec FROM chunks").fetchall():
        if blob is None or p not in paths:
            continue
        v = np.frombuffer(blob, dtype=np.float32)
        # Match the ACTIVE embed backend's width (1024 qwen3 / 1536 cohere), not a
        # static constant — else the semantic map is blank in cloud/cohere mode.
        if v.size == store._active_dim():
            by_path.setdefault(p, []).append(v)
    out: dict = {}
    for p, vs in by_path.items():
        m = np.mean(np.vstack(vs), axis=0)
        n = float(np.linalg.norm(m))
        out[p] = (m / n) if n else m
    return out


def _project_2d(mat: np.ndarray) -> np.ndarray:
    """Project (n, d) vectors to (n, 2) normalized to [0,1] per axis. UMAP
    (nonlinear, best neighborhood/cluster separation) when there are enough
    points and it imports; PCA fallback; circle for tiny n. Deterministic."""
    n = int(mat.shape[0])
    if n == 0:
        return np.zeros((0, 2), dtype=np.float32)
    if n == 1:
        return np.array([[0.5, 0.5]], dtype=np.float32)
    coords = None
    if n >= 5:
        try:
            import umap

            coords = umap.UMAP(
                n_neighbors=min(15, n - 1), n_components=2, metric="cosine", random_state=42
            ).fit_transform(mat)
        except Exception as e:  # noqa: BLE001 — optional dep / any failure -> PCA
            log.debug("UMAP unavailable (%s); falling back to PCA", e)
    if coords is None:
        try:
            from sklearn.decomposition import PCA

            coords = PCA(n_components=2, random_state=42).fit_transform(mat)
        except Exception as e:  # noqa: BLE001
            log.debug("PCA failed (%s); using a circle layout", e)
            ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
            coords = np.column_stack([np.cos(ang), np.sin(ang)])
    coords = np.asarray(coords, dtype=np.float32)
    mn, mx = coords.min(axis=0), coords.max(axis=0)
    span = np.where((mx - mn) > 1e-9, mx - mn, 1.0)
    return (coords - mn) / span


def umap_graph_impl(ws_path: str, max_edges: int = 300) -> dict:
    """File graph laid out by a 2D embedding projection (the "semantic map"):
    same nodes/edges/community as ``file_graph`` plus per-node ``ux``/``uy`` in
    [0,1]. Files placed close together are similar in MEANING even when they
    share no entities (and thus have no co-occurrence edge). UMAP projection of
    the per-file mean Qwen3 vector, with a PCA fallback."""
    g = store.file_graph(ws_path, max_edges=max_edges)
    want = {n["id"] for n in g["nodes"]}
    if want:
        conn = store._connect(ws_path)
        try:
            vecs = _file_mean_vectors(conn, want)
        finally:
            conn.close()
        present = [n["id"] for n in g["nodes"] if n["id"] in vecs]
        if present:
            coords = _project_2d(np.vstack([vecs[p] for p in present]))
            xy = {
                p: (round(float(coords[i][0]), 4), round(float(coords[i][1]), 4))
                for i, p in enumerate(present)
            }
            for n in g["nodes"]:
                if n["id"] in xy:
                    n["ux"], n["uy"] = xy[n["id"]]
    g["layout"] = "umap"
    return g


def all_workspaces_umap_graph(max_edges: int = 400) -> dict:
    """Cross-workspace semantic map: the unified all-workspaces graph laid out by a
    single UMAP projection of every indexed file's mean vector, so files from
    different folders that are similar in MEANING land near each other. Node ids
    are absolute paths (as in ``all_workspaces_graph``). Only files whose vectors
    match the active embed dim are projected (a mixed-backend corpus places just
    the matching ones)."""
    g = all_workspaces_graph(max_edges)
    want = {n["id"] for n in g["nodes"]}
    if not want:
        g["layout"] = "umap"
        return g
    vecs: dict[str, np.ndarray] = {}
    for ws in store.list_indexed_workspaces():
        # Skip folders with no active-backend index (see all_workspaces_graph):
        # _connect would fabricate an empty db for an other-backend index.
        if not store.has_index(ws):
            continue
        root = os.path.normpath(os.path.abspath(os.path.expanduser(ws)))
        try:
            conn = store._connect(ws)
        except Exception:
            continue
        try:
            # Map this workspace's wanted files (relative in the DB) to abs ids.
            rel_to_abs = {
                os.path.relpath(ap, root): ap for ap in want if ap.startswith(root + os.sep)
            }
            for rel, m in _file_mean_vectors(conn, set(rel_to_abs)).items():
                vecs[rel_to_abs[rel]] = m
        finally:
            conn.close()
    present = [n["id"] for n in g["nodes"] if n["id"] in vecs]
    if present:
        coords = _project_2d(np.vstack([vecs[p] for p in present]))
        xy = {
            p: (round(float(coords[i][0]), 4), round(float(coords[i][1]), 4))
            for i, p in enumerate(present)
        }
        for n in g["nodes"]:
            if n["id"] in xy:
                n["ux"], n["uy"] = xy[n["id"]]
    g["layout"] = "umap"
    return g
