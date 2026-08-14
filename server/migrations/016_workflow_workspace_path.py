"""Record the canonical workspace a workflow run was pinned to at launch.

A run previously had no notion of its own workspace root: every child agent
resolved whatever was GLOBALLY connected when it happened to start, so
switching workspaces mid-run could redirect later agents to a different repo.
Migration 009's CREATE includes the column directly, so this only widens
tables created before the column existed and is a no-op on fresh databases.
"""

import sqlite3

VERSION = 16
DESCRIPTION = "workflow_runs: add workspace_path"


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_runs)")}
    if not cols:
        return  # table not created yet; 009 creates the new shape
    if "workspace_path" not in cols:
        conn.execute("ALTER TABLE workflow_runs ADD COLUMN workspace_path TEXT")
