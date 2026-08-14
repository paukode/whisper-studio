"""Record the effort level a workflow run was launched at.

A run already carried its effort in memory (WorkflowRun.effort_label) but never
stored it, so a resume had to guess and the Runs list could not show what a run
actually reasoned at. Migration 009's CREATE includes the column directly (it
also serves as the lazy bootstrapper in server/workflows/manager.py), so this
only widens tables created before the column existed and is a no-op on fresh
databases.
"""

import sqlite3

VERSION = 15
DESCRIPTION = "workflow_runs: add effort_label"


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_runs)")}
    if not cols:
        return  # table not created yet; 009 creates the new shape
    if "effort_label" not in cols:
        conn.execute("ALTER TABLE workflow_runs ADD COLUMN effort_label TEXT")
