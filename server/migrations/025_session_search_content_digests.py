"""Rebuild the session search mirror with content-digest signatures.

The incremental reindex compared each message by (role, text length, tool
length) and each transcript segment by its length, so an edit that kept the
length left the old text in the index: search found text that no longer
existed and missed the new text. Signatures are now content digests. A
session's next save reindexes it from the start (its stored signatures no
longer match); this rebuild fixes the sessions that are never saved again.
"""

import sqlite3

VERSION = 25
DESCRIPTION = "Rebuild the session search mirror with content-digest signatures"


def migrate(conn: sqlite3.Connection) -> None:
    from server.infrastructure.session_search import ensure_fts, rebuild_all

    ensure_fts(conn)
    rebuild_all(conn)
