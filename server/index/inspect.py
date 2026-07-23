"""Manual index inspector.

    python -m server.index.inspect                 # list indexed workspaces, show the first
    python -m server.index.inspect /path/to/folder # show that workspace's index

Reads the SQLite index directly and prints the stats, the per-file chunk counts,
and a sample file broken into its chunks + extracted graph entities — so you can
verify what got indexed without the UI.
"""

import sqlite3
import sys

from . import paths, store


def show(ws: str) -> None:
    if not paths.is_indexed(ws):
        print(f"Not indexed: {ws}")
        return
    s = store.stats(ws)
    print(f"Workspace:  {ws}")
    print(f"DB:         {paths.db_path(ws)}  (vectors stored per-chunk as BLOBs inside it)")
    print(f"Indexed:    {s['files']} files, {s['chunks']} chunks, {s['nodes']} entities")
    print(f"Last build: {s.get('last_indexed_at')}   embed_model={s.get('embed_model')}")

    conn = sqlite3.connect(paths.db_path(ws))
    try:
        print("\nFiles (chunks each):")
        for path, n in conn.execute("SELECT path, n_chunks FROM files ORDER BY path").fetchall():
            print(f"  {n:>4}  {path}")

        row = conn.execute("SELECT path FROM chunks ORDER BY id LIMIT 1").fetchone()
        if not row:
            return
        sample = row[0]
        print(f"\nSample file → chunk/entity structure: {sample}")
        chunks = conn.execute(
            "SELECT id, start_line, end_line, substr(text, 1, 200) "
            "FROM chunks WHERE path=? ORDER BY id LIMIT 3",
            (sample,),
        ).fetchall()
        for cid, sl, el, text in chunks:
            ents = conn.execute(
                "SELECT nodes.name, nodes.label FROM node_chunks "
                "JOIN nodes ON nodes.id = node_chunks.node_id "
                "WHERE node_chunks.chunk_id=?",
                (cid,),
            ).fetchall()
            ent_str = ", ".join(f"{n} ({lbl})" for n, lbl in ents) or "(none)"
            print(f"  chunk #{cid}  lines {sl}-{el}")
            print(f"    text:     {' '.join(text.split())[:140]!r}")
            print(f"    entities: {ent_str}")
    finally:
        conn.close()


def main(argv: list[str]) -> None:
    if argv:
        show(argv[0])
        return
    workspaces = store.list_indexed_workspaces()
    if not workspaces:
        print(f"No indexed workspaces under {paths.INDEX_DATA_DIR}")
        return
    print("Indexed workspaces:")
    for w in workspaces:
        print(f"  - {w}")
    print()
    show(workspaces[0])


if __name__ == "__main__":
    main(sys.argv[1:])
