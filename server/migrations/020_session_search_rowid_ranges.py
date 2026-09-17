"""Rebuild the session search mirror with per-session rowid ranges.

The first mirror (migration 019) rewrote a session's whole index on every
save: delete every row of the session, tokenize every message again. On a
long agentic session that ran for seconds per save and competed with every
other request while a turn streamed. The mirror now assigns each session a
numeric slot (session_fts_state) and derives every row's rowid from that slot
and the message index, so a save touches only the rows that changed and the
old rows are deleted by rowid range instead of a table scan. Existing rows
carry arbitrary rowids, so the mirror is rebuilt once here.
"""

import sqlite3

VERSION = 20
DESCRIPTION = "Rebuild the session search mirror with per-session rowid ranges"


def migrate(conn: sqlite3.Connection) -> None:
    from server.infrastructure.session_search import ensure_fts, rebuild_all

    if ensure_fts(conn):
        rebuild_all(conn)
