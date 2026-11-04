"""Voice mode is gone: its cost rows become unattributed.

The hands-free voice mode (Amazon Nova 2 Sonic) was removed, and with it the
``voice`` cost source. Rows it wrote, and the old per-run ``voice-`` ids
migration 021 labelled, now carry ``unattributed`` so no row names a source
the app no longer has.
"""

import sqlite3

VERSION = 26
DESCRIPTION = "Relabel voice cost rows as unattributed"


def migrate(conn: sqlite3.Connection) -> None:
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "session_costs" not in tables:
        return
    columns = [r[1] for r in conn.execute("PRAGMA table_info(session_costs)").fetchall()]
    if "source" not in columns:
        return
    conn.execute("UPDATE session_costs SET source = 'unattributed' WHERE source = 'voice'")
