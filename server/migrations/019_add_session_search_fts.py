"""Full-text search mirror of session messages (session_search).

The model could list session titles and a user could @mention one session,
but nothing could search the CONTENT of past conversations without an LLM
call. This FTS5 table mirrors every session's prompt-role messages and is
kept in step by the sessions store on every save, append, and delete
(server/infrastructure/session_search.py). Backfilled here from the existing
rows so search works for history that predates the table.
"""

import sqlite3

VERSION = 19
DESCRIPTION = "Add FTS5 mirror of session messages for session_search"


def migrate(conn: sqlite3.Connection) -> None:
    from server.infrastructure.session_search import ensure_fts, rebuild_all

    if ensure_fts(conn):
        rebuild_all(conn)
