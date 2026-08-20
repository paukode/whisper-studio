"""Words-first explorer views over a workspace index.

These power the index explorer UI (browse pane, overview topic cards, entity
pages, file pages) — every answer is names, sentences, and counts, never an
unlabeled visual encoding. Like graph_views, they are *recomputable* read-time
views over the same SQLite store, split into their own module for the file-size
budget; the public entry points are thin cached wrappers on ``store``.

Scope note: everything here is per-workspace and derived from existing tables
(nodes, node_chunks, chunks, files, relations2) — no new index passes.
"""

from __future__ import annotations

import math
import os

from server.index import relstore, store
from server.index.config import SALIENCE_GRAPH_FLOOR

# Browse-pane grouping: entity labels folded into user-words buckets. Any label
# not listed reads as a topic (metrics, technologies, products, and the whole
# code-profile label set all belong there).
#
# "job title" is deliberately NOT people: a title is a role, and mixing the two
# put rows like "EDITOR" and "CEO" under People, where a reader expects names.
_LABEL_GROUPS: dict[str, str] = {
    "person": "people",
    "job title": "roles",
    "role": "roles",
    "organization": "organizations",
    "company": "organizations",
    "team": "organizations",
    "location": "places",
    "place": "places",
}
GROUP_ORDER = ("people", "organizations", "roles", "topics", "places")
GROUP_TITLES = {
    "people": "People",
    "organizations": "Organizations",
    "roles": "Roles and titles",
    "topics": "Topics",
    "places": "Places",
}


def _group_for(label: str) -> str:
    return _LABEL_GROUPS.get((label or "").strip().lower(), "topics")


def looks_like_person_name(name: str) -> bool:
    """Whether a name plausibly names a human.

    Entity extraction is zero-shot: the model is handed a label list and guesses
    which span fits which label, with no gazetteer of real names. On technical
    documents it reliably mislabels two shapes as people: template metadata keys
    ("AUTHOR:" header lines) and ALL-CAPS field identifiers (SAP columns like
    ERNAM or KUNNR). Both are single tokens with no case variation, whereas a
    human name written in a document is mixed case ("Dana Kim", "McCarthy").

    Multi-word names pass. Scripts without upper/lower case (CJK, for instance)
    carry no signal here, so they pass rather than being punished for it.
    """
    n = (name or "").strip()
    if not n:
        return False
    if " " in n or "-" in n or "." in n:
        return True
    cased = [c for c in n if c.isupper() or c.islower()]
    if not cased:
        return True
    return any(c.isupper() for c in n) and any(c.islower() for c in n)


def _is_low_signal(entity: dict) -> bool:
    """Whether an entity should sit behind the explorer's "show low-signal
    names" toggle: below the salience floor, or a person-labelled row whose name
    is not shaped like a human name (see looks_like_person_name). Demoted rather
    than dropped, so the count stays honest and the rows stay reachable."""
    if entity["salience"] < SALIENCE_GRAPH_FLOOR:
        return True
    if _group_for(entity["label"]) == "people" and not looks_like_person_name(entity["name"]):
        return True
    return False


def _like_escape(q: str) -> str:
    """Escape LIKE metacharacters so a literal query never becomes a wildcard."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _entity_rollup(conn) -> list[dict]:
    """Every entity, case-folded to one row per distinct name. Counts (distinct
    files, distinct mention chunks, best salience) aggregate across case/label
    variants in SQL so overlapping variants never double-count; the display
    spelling and label come from the most-mentioned stored variant (same rule as
    the entity page's canonical resolution). Generic category words are dropped
    outright (pure noise)."""
    generic = sorted(store._GENERIC_ENTITY_NAMES)
    gph = ",".join("?" for _ in generic)
    acc = conn.execute(
        f"""SELECT ulower(n.name) AS key,
                   MAX(COALESCE(n.salience, 0.5)) AS sal,
                   COUNT(DISTINCT c.path) AS files,
                   COUNT(DISTINCT nc.chunk_id) AS mentions
            FROM nodes n
            JOIN node_chunks nc ON nc.node_id = n.id
            JOIN chunks c ON c.id = nc.chunk_id
            WHERE ulower(n.name) NOT IN ({gph})
            GROUP BY ulower(n.name)""",
        generic,
    ).fetchall()
    disp_rows = conn.execute(
        f"""SELECT n.name, n.label, MAX(COALESCE(n.description, '')) AS description,
                   COUNT(nc.chunk_id) AS mentions
            FROM nodes n
            JOIN node_chunks nc ON nc.node_id = n.id
            WHERE ulower(n.name) NOT IN ({gph})
            GROUP BY n.name, n.label""",
        generic,
    ).fetchall()
    disp: dict[str, tuple] = {}
    desc_by_key: dict[str, str] = {}
    for name, label, description, mentions in disp_rows:
        key = (name or "").strip().lower()
        if not key:
            continue
        if key not in disp or mentions > disp[key][2]:
            disp[key] = (name, label or "", mentions)
        if description and not desc_by_key.get(key):
            desc_by_key[key] = description
    out = []
    for key, sal, files, mentions in acc:
        key = (key or "").strip()
        if not key or key not in disp:
            continue
        name, label, _m = disp[key]
        out.append(
            {
                "name": name,
                "label": label,
                "salience": round(float(sal), 4),
                "files": files,
                "mentions": mentions,
                "description": desc_by_key.get(key, ""),
            }
        )
    return out


def entity_list_impl(ws_path: str, q: str = "", include_low: bool = False) -> dict:
    """The browse pane / entity search: entities grouped into People,
    Organizations, Roles and titles, Topics, and Places, ranked by distinct-file
    count. Low-signal names (below the salience floor, or person rows not shaped
    like human names) are hidden by default but counted honestly (``hidden``),
    and ``include_low`` brings them back. A non-empty ``q`` filters by
    substring, case-insensitive."""
    conn = store._connect(ws_path)
    try:
        ents = _entity_rollup(conn)
    finally:
        conn.close()
    needle = (q or "").strip().lower()
    if needle:
        ents = [e for e in ents if needle in e["name"].lower()]
    visible = [e for e in ents if not _is_low_signal(e)]
    hidden = len(ents) - len(visible)
    if include_low:
        visible = ents
    groups: dict[str, list] = {g: [] for g in GROUP_ORDER}
    for e in visible:
        groups[_group_for(e["label"])].append(e)
    out_groups = []
    for g in GROUP_ORDER:
        items = sorted(groups[g], key=lambda e: (-e["files"], -e["mentions"], e["name"].lower()))
        out_groups.append({"key": g, "title": GROUP_TITLES[g], "entities": items})
    return {"groups": out_groups, "total": len(visible), "hidden": hidden}


def _topic_cards(conn, graph: dict) -> list[dict]:
    """Name each Leiden community with its most *distinctive* entities: score =
    files-in-group × salience × log(1 + N_files / files-total), so a name that
    saturates the whole folder can't title every card, and names are greedily
    deduped across cards (largest group picks first). Cards claim co-mention
    ("files mentioning: …"), never aboutness — the frontend copy keeps that
    honest. Isolated files (community -1) get no card."""
    by_comm: dict[int, list[dict]] = {}
    for n in graph["nodes"]:
        ci = n.get("community", -1)
        if ci is not None and ci >= 0:
            by_comm.setdefault(ci, []).append(n)
    if not by_comm:
        return []
    n_files = max(len(graph["nodes"]), 1)
    generic = sorted(store._GENERIC_ENTITY_NAMES)
    gph = ",".join("?" for _ in generic)
    # files-total per entity name (corpus-wide), for the distinctiveness term.
    df_files = {
        (nm or "").strip().lower(): df
        for nm, df in conn.execute(
            """SELECT n.name, COUNT(DISTINCT c.path)
               FROM nodes n
               JOIN node_chunks nc ON nc.node_id = n.id
               JOIN chunks c ON c.id = nc.chunk_id
               GROUP BY ulower(n.name)"""
        ).fetchall()
    }
    cards = []
    used_names: set[str] = set()
    for ci, members in sorted(by_comm.items(), key=lambda kv: -len(kv[1])):
        if len(members) < 2:
            continue
        paths = [m["id"] for m in members]
        pph = ",".join("?" for _ in paths)
        rows = conn.execute(
            f"""SELECT n.name, MAX(COALESCE(n.salience, 0.5)) AS sal,
                       COUNT(DISTINCT c.path) AS f_in
                FROM nodes n
                JOIN node_chunks nc ON nc.node_id = n.id
                JOIN chunks c ON c.id = nc.chunk_id
                WHERE c.path IN ({pph}) AND ulower(n.name) NOT IN ({gph})
                  AND COALESCE(n.salience, 0.5) >= ?
                GROUP BY ulower(n.name)""",
            [*paths, *generic, SALIENCE_GRAPH_FLOOR],
        ).fetchall()
        scored = []
        for nm, sal, f_in in rows:
            key = (nm or "").strip().lower()
            if not key or key in used_names:
                continue
            total = max(df_files.get(key, f_in), 1)
            scored.append((f_in * float(sal) * math.log(1 + n_files / total), nm))
        scored.sort(key=lambda t: -t[0])
        names = [nm for _, nm in scored[:3]]
        used_names.update(nm.lower() for nm in names)
        top = sorted(members, key=lambda m: (-(m.get("degree") or 0), m["name"].lower()))
        cards.append(
            {
                "id": ci,
                "names": names,
                "files": len(members),
                "top_files": [{"path": m["id"], "name": m["name"]} for m in top[:5]],
            }
        )
    return cards


def overview_impl(ws_path: str) -> dict:
    """The explorer's landing view: inventory stats, one named topic card per
    detected file group, the most salient entities, and the most connected
    files. Reuses the cached file graph for communities and degree."""
    stats = store.stats(ws_path)
    graph = store.file_graph(ws_path)
    conn = store._connect(ws_path)
    try:
        cards = _topic_cards(conn, graph)
        ents = _entity_rollup(conn)
    finally:
        conn.close()
    visible = [e for e in ents if not _is_low_signal(e)]
    stands_out = sorted(visible, key=lambda e: (-e["salience"], -e["files"]))[:8]
    connected = sorted(
        (n for n in graph["nodes"] if (n.get("degree") or 0) > 0),
        key=lambda n: -(n.get("degree") or 0),
    )[:8]
    return {
        "files": stats.get("files", 0),
        "passages": stats.get("chunks", 0),
        "entities": len(visible),
        "last_indexed_at": stats.get("last_indexed_at"),
        "cards": cards,
        "stands_out": [
            {"name": e["name"], "label": e["label"], "files": e["files"]} for e in stands_out
        ],
        "most_connected": [
            {"path": n["id"], "name": n["name"], "links": n.get("degree") or 0} for n in connected
        ],
        "truncated": bool(graph.get("truncated")),
    }


def entity_page_impl(ws_path: str, name: str, label: str = "") -> dict:
    """One entity's page: canonical spelling + label + description, aggregated
    typed facts (sentence-ready, with verbatim evidence and line-anchored
    citations), the files that mention it, and its most co-mentioned
    neighbours."""
    name = (name or "").strip()
    if not name:
        return {"found": False}
    key = name.lower()
    generic = sorted(store._GENERIC_ENTITY_NAMES)
    gph = ",".join("?" for _ in generic)
    conn = store._connect(ws_path)
    try:
        # Canonical display variant: the most common stored (name, label) whose
        # name case-folds to the query (same rule the old entity pivot used).
        params: list = [key]
        label_clause = ""
        if label:
            label_clause = " AND label = ?"
            params.append(label)
        nr = conn.execute(
            f"SELECT name, label, description, COUNT(*) c FROM nodes WHERE ulower(name)=?"
            f"{label_clause} GROUP BY name, label ORDER BY c DESC LIMIT 1",
            params,
        ).fetchone()
        if not nr:
            return {"found": False}
        mention_rows = conn.execute(
            f"""SELECT c.path, COUNT(DISTINCT nc.chunk_id) AS w
                FROM nodes n
                JOIN node_chunks nc ON nc.node_id = n.id
                JOIN chunks c ON c.id = nc.chunk_id
                WHERE ulower(n.name) = ?{label_clause.replace("label", "n.label")}
                GROUP BY c.path ORDER BY w DESC, c.path LIMIT 200""",
            params,
        ).fetchall()
        related = conn.execute(
            f"""SELECT n2.name, n2.label,
                       MAX(COALESCE(n2.salience, 0.5)) AS sal,
                       COUNT(DISTINCT nc2.chunk_id) AS shared
                FROM nodes n
                JOIN node_chunks nc ON nc.node_id = n.id
                JOIN node_chunks nc2 ON nc2.chunk_id = nc.chunk_id AND nc2.node_id != n.id
                JOIN nodes n2 ON n2.id = nc2.node_id
                WHERE ulower(n.name) = ? AND ulower(n2.name) != ?
                  AND ulower(n2.name) NOT IN ({gph})
                  AND COALESCE(n2.salience, 0.5) >= ?
                GROUP BY ulower(n2.name)
                ORDER BY shared DESC, sal DESC LIMIT 12""",
            [key, key, *generic, SALIENCE_GRAPH_FLOOR],
        ).fetchall()
    finally:
        conn.close()
    facts = relstore.facts_for_entity(ws_path, name, limit=30)
    for f in facts:
        cite = f.get("cite") or {}
        if cite.get("path"):
            cite["name"] = os.path.basename(cite["path"])
    return {
        "found": True,
        "name": nr[0],
        "label": nr[1] or "",
        "description": nr[2] or "",
        "facts": facts,
        "mentioned_in": [
            {"path": p, "name": os.path.basename(p), "passages": w} for p, w in mention_rows
        ],
        "related": [{"name": nm, "label": lb or "", "shared": sh} for nm, lb, _sal, sh in related],
        "truncated": len(mention_rows) >= 200,
    }


def file_page_impl(ws_path: str, file_path: str, max_neighbors: int = 30) -> dict:
    """One file's page: the names and topics it mentions, every file that shares
    salient entities with it (computed directly for THIS file, so it is never
    clipped by the whole-graph edge cap), and the typed facts stated in it."""
    # Callers hand over tree-relative paths in varied spellings ("./runbook.md",
    # "a//b.md"); the files table stores them normalized ("runbook.md").
    file_path = os.path.normpath((file_path or "").strip())
    if not file_path or file_path == ".":
        return {"found": False}
    generic = sorted(store._GENERIC_ENTITY_NAMES)
    gph = ",".join("?" for _ in generic)
    conn = store._connect(ws_path)
    try:
        fr = conn.execute("SELECT n_chunks FROM files WHERE path = ?", (file_path,)).fetchone()
        if not fr:
            return {"found": False}
        ent_rows = conn.execute(
            f"""SELECT n.name, n.label,
                       MAX(COALESCE(n.salience, 0.5)) AS sal,
                       COUNT(DISTINCT nc.chunk_id) AS mentions
                FROM nodes n
                JOIN node_chunks nc ON nc.node_id = n.id
                JOIN chunks c ON c.id = nc.chunk_id
                WHERE c.path = ? AND ulower(n.name) NOT IN ({gph})
                GROUP BY ulower(n.name)
                HAVING MAX(COALESCE(n.salience, 0.5)) >= ?
                ORDER BY sal DESC, mentions DESC LIMIT 40""",
            [file_path, *generic, SALIENCE_GRAPH_FLOOR],
        ).fetchall()
        # Shared-entity neighbours, anchored on this file. Same "good entity"
        # filter as file_graph (salience floor, fanout bound, generic stoplist)
        # but with no global edge cap — the page must show this file's whole
        # neighbourhood even when the overview graph is truncated. One row per
        # (neighbour, shared node), aggregated in Python: entity names may
        # contain commas, so no GROUP_CONCAT.
        nb_rows = conn.execute(
            f"""WITH freq AS (
                   SELECT node_id, COUNT(*) AS cnt FROM node_chunks GROUP BY node_id
               ),
               good AS (
                   SELECT nc.node_id, nc.chunk_id FROM node_chunks nc
                   JOIN freq f ON f.node_id = nc.node_id
                   JOIN nodes nn ON nn.id = nc.node_id
                   WHERE f.cnt <= ? AND COALESCE(nn.salience, 0.5) >= ?
                     AND ulower(nn.name) NOT IN ({gph})
               ),
               mine AS (
                   SELECT DISTINCT g.node_id FROM good g
                   JOIN chunks c ON c.id = g.chunk_id WHERE c.path = ?
               )
               SELECT DISTINCT c2.path, g2.node_id, nd.name,
                      COALESCE(nd.salience, 0.5) AS sal
               FROM mine m
               JOIN good g2 ON g2.node_id = m.node_id
               JOIN chunks c2 ON c2.id = g2.chunk_id
               JOIN nodes nd ON nd.id = g2.node_id
               WHERE c2.path != ?""",
            [store._MAX_NODE_FANOUT, SALIENCE_GRAPH_FLOOR, *generic, file_path, file_path],
        ).fetchall()
        fact_rows = conn.execute(
            """SELECT sn.name, tn.name, r.predicate, r.strength,
                      r.start_line, r.end_line, r.evidence
               FROM relations2 r
               JOIN nodes sn ON sn.id = r.src_node_id
               JOIN nodes tn ON tn.id = r.tgt_node_id
               WHERE r.path = ?""",
            (file_path,),
        ).fetchall()
    finally:
        conn.close()
    # Group the (neighbour, shared node) rows: weight = distinct shared nodes,
    # display sample = the most salient shared names.
    by_path: dict[str, dict] = {}
    for p, nid, nm, sal in nb_rows:
        e = by_path.setdefault(p, {"ids": set(), "names": []})
        if nid not in e["ids"]:
            e["ids"].add(nid)
            e["names"].append((-float(sal), nm))
    ranked = sorted(by_path.items(), key=lambda kv: (-len(kv[1]["ids"]), kv[0]))
    neighbors = []
    for p, e in ranked[:max_neighbors]:
        names = [nm for _, nm in sorted(e["names"])][: store._EDGE_ENTITY_SAMPLE]
        neighbors.append(
            {"path": p, "name": os.path.basename(p), "shared": len(e["ids"]), "entities": names}
        )
    # One fact per (source, predicate, target): strength is the max across its
    # rows, evidence the longest verbatim quote (accumulated independently, so
    # a weak row with a long quote can't drag the strength down).
    facts_by_key: dict[tuple, dict] = {}
    for s, t, pred, strength, sl, el, ev in fact_rows:
        fkey = (s.lower(), pred, t.lower())
        cur = facts_by_key.get(fkey)
        if cur is None:
            facts_by_key[fkey] = {
                "source": s,
                "target": t,
                "predicate": pred,
                "strength": round(float(strength), 1),
                "start_line": sl,
                "end_line": el,
                "evidence": ev,
            }
            continue
        cur["strength"] = max(cur["strength"], round(float(strength), 1))
        if len(ev or "") > len(cur.get("evidence") or ""):
            cur.update(evidence=ev, start_line=sl, end_line=el)
    facts = sorted(facts_by_key.values(), key=lambda f: -f["strength"])[:30]
    return {
        "found": True,
        "path": file_path,
        "name": os.path.basename(file_path),
        "passages": fr[0] or 0,
        "entities": [
            {"name": nm, "label": lb or "", "mentions": m} for nm, lb, _sal, m in ent_rows
        ],
        "neighbors": neighbors,
        "facts": facts,
        "neighbors_total": len(ranked),
        "neighbors_truncated": len(ranked) > max_neighbors,
    }
