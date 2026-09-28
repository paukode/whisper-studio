"""Drop the two sessions columns nothing ever wrote.

Migration 002 added compaction_count and latched_config beside
workspace_path, but no code path ever stored a value in either: compactions
are logged per session under data_root()/compactions (server/chat/
compaction_log.py), and the session latch lives only in memory
(latch_session in server/infrastructure/config.py). Every row held the
defaults and the client never read them.

A column that is already gone is skipped, so a re-run is a no-op.
"""

import sqlite3

VERSION = 23
DESCRIPTION = "sessions: drop the unused compaction_count and latched_config columns"


def migrate(conn: sqlite3.Connection) -> None:
    present = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    for column in ("compaction_count", "latched_config"):
        if column in present:
            conn.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
