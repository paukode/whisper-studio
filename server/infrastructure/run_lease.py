"""Run leases: one terminal write per run, the first writer wins.

A run row (cron_runs, workflow_runs) is written as ``running`` when the run
starts. More than one writer can end it: the natural finish, a user stop, a
backstop after a failed write. Every one of them goes through
``finish_if_running``. Its UPDATE only matches a row that is still
``running``, so SQLite gives the row to exactly one writer, and only that
writer records the outcome and announces it.
"""

from __future__ import annotations

import sqlite3

RUNNING = "running"


def finish_if_running(
    conn: sqlite3.Connection, table: str, key_column: str, key: str, fields: dict
) -> bool:
    """Write ``fields`` to the row ``key`` of ``table`` only while that row is
    still ``running``. Returns True when this call made the terminal write.

    ``table``, ``key_column`` and the field names come from code, never from
    input, and are checked as identifiers because SQL cannot bind them.
    """
    if not fields:
        raise ValueError("finish_if_running needs at least one field to write")
    names = [table, key_column, *fields]
    bad = [n for n in names if not n.isidentifier()]
    if bad:
        raise ValueError(f"not an SQL identifier: {bad[0]!r}")
    assignments = ", ".join(f"{name}=?" for name in fields)
    cur = conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {key_column}=? AND status=?",
        (*fields.values(), key, RUNNING),
    )
    return cur.rowcount > 0
