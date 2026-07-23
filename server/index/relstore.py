"""Node-id-keyed typed relations (the ``relations2`` table): storage + queries.

Split out of ``store`` to keep it under the file-size budget. Relations are keyed
by node id (so entity dedup repoints them instead of dangling — see
store._merge_group), scoped by source file ``path`` for incremental replace, and
carry optional verbatim evidence + line provenance so a fact is citable. Facts are
aggregated across files at read time (noisy-OR over observations), so the same
fact stated in several files reinforces confidence rather than duplicating.
"""

from __future__ import annotations

import hashlib

from . import store


def evidence_line(text: str, source: str, target: str) -> tuple[int | None, int | None, str | None]:
    """Best-effort verbatim evidence for a relation: the first line where both
    entity names co-occur (else the first line naming the source). Returns
    ``(start_line, end_line, evidence)`` — 1-based line numbers over the same text
    the chunker used, so the citation's &L anchor lands correctly — or all None.
    Purely lexical; no LLM."""
    if not text:
        return (None, None, None)
    s, t = source.lower(), target.lower()
    src_only: int | None = None
    for i, ln in enumerate(text.splitlines()):
        low = ln.lower()
        if s in low and t in low:
            return (i + 1, i + 1, ln.strip()[:200])
        if src_only is None and s in low:
            src_only = i
    if src_only is not None:
        return (src_only + 1, src_only + 1, text.splitlines()[src_only].strip()[:200])
    return (None, None, None)


def _evidence_hash(evidence: str, path: str, s: int, pred: str, t: int) -> str:
    """Dedup key for a stored fact WITHIN one file. Always keyed on the file path
    (plus the fact and its verbatim quote) so each file keeps its own row and
    delete-per-path never orphans a fact that another file also asserts — cross-file
    dedup happens at read time in facts_for_entity (grouped by predicate + other
    entity). Two identical quotes for the same fact in one file still collapse."""
    quote = " ".join((evidence or "").split()).lower()
    basis = f"{path}|{s}|{pred}|{t}|{quote}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def set_file_relations_v2(ws_path: str, path: str, facts: list[dict]) -> int:
    """Replace ``relations2`` rows for one file. ``facts`` items are dicts with
    entity NAMES (resolved to node ids in this connection): ``{source, target,
    predicate, strength, evidence?, start_line?, end_line?}``. Returns rows written.
    Nodes are expected to already exist (the pipeline calls this right after the
    file's chunks/entities are stored)."""
    conn = store._connect(ws_path)
    n = 0
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute("DELETE FROM relations2 WHERE path=?", (path,))
        id_by_name: dict[str, int] = {}
        for nid, name in cur.execute("SELECT id, name FROM nodes").fetchall():
            key = (name or "").lower()
            if key not in id_by_name:  # first (lowest) id wins, deterministic
                id_by_name[key] = nid
        for f in facts:
            s = id_by_name.get(str(f.get("source", "")).strip().lower())
            t = id_by_name.get(str(f.get("target", "")).strip().lower())
            pred = str(f.get("predicate", "")).strip()
            if not (s and t and pred) or s == t:
                continue
            try:
                strength = max(1.0, min(float(f.get("strength", 3.0)), 5.0))
            except (TypeError, ValueError):
                strength = 3.0
            ev = (f.get("evidence") or "")[:200] or None
            cur.execute(
                "INSERT OR IGNORE INTO relations2"
                "(src_node_id, tgt_node_id, predicate, strength, path, start_line, "
                "end_line, evidence, evidence_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    s,
                    t,
                    pred,
                    strength,
                    path,
                    f.get("start_line"),
                    f.get("end_line"),
                    ev,
                    _evidence_hash(ev, path, s, pred, t),
                ),
            )
            n += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    store._invalidate(ws_path)
    return n


def facts_for_entity(
    ws_path: str, name: str, limit: int = 15, predicate: str | None = None
) -> list[dict]:
    """Aggregated typed facts touching an entity (matched by name, any label).

    Returns ``[{predicate, other, direction ('out'|'in'), score, sources, cite}]``
    where ``score`` is a noisy-OR over up to 5 observations and ``cite`` is the
    best (most-evidenced) source ``{path, start_line, end_line, evidence}``,
    ordered by score. ``direction`` 'out' = self→other, 'in' = other→self."""
    conn = store._connect(ws_path)
    try:
        nids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM nodes WHERE LOWER(name)=?", (name.strip().lower(),)
            ).fetchall()
        ]
        if not nids:
            return []
        marks = ",".join("?" * len(nids))
        rows = conn.execute(
            f"""SELECT r.src_node_id, r.tgt_node_id, r.predicate, r.strength, r.path,
                       r.start_line, r.end_line, r.evidence, sn.name, tn.name
                FROM relations2 r
                JOIN nodes sn ON sn.id = r.src_node_id
                JOIN nodes tn ON tn.id = r.tgt_node_id
                WHERE r.src_node_id IN ({marks}) OR r.tgt_node_id IN ({marks})""",
            nids + nids,
        ).fetchall()
    finally:
        conn.close()
    selfset = set(nids)
    agg: dict[tuple, dict] = {}
    for sid, _tid, pred, strength, path, sl, el, ev, sname, tname in rows:
        if predicate and pred != predicate:
            continue
        if sid in selfset:
            other, direction = tname, "out"
        else:
            other, direction = sname, "in"
        key = (direction, pred, (other or "").lower())
        a = agg.setdefault(
            key,
            {"predicate": pred, "other": other, "direction": direction, "probs": [], "cite": None},
        )
        a["probs"].append(min(max(strength / 5.0, 0.0), 1.0))
        cur_ev = (a["cite"] or {}).get("evidence") or ""
        if a["cite"] is None or len(ev or "") > len(cur_ev):
            a["cite"] = {"path": path, "start_line": sl, "end_line": el, "evidence": ev}
    out = []
    for a in agg.values():
        p = 1.0
        for x in a["probs"][:5]:
            p *= 1.0 - x
        out.append(
            {
                "predicate": a["predicate"],
                "other": a["other"],
                "direction": a["direction"],
                "score": round(1.0 - p, 3),
                "sources": len(a["probs"]),
                "cite": a["cite"],
            }
        )
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:limit]
