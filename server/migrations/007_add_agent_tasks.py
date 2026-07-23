"""Unified background-task registry.

One table for every long-running unit of work the server owns: background
shell commands (today's server/background_tasks.py in-memory dict), detached
agent runs, and future workflow runs. Rows carry a lease (status='running' +
owner_pid) so work interrupted by a restart is reconciled on boot instead of
hanging forever, and shell tasks whose child process survived the restart can
be re-adopted rather than falsely failed.
"""

import sqlite3

VERSION = 7
DESCRIPTION = "Add agent_tasks table for the unified background-task registry"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tasks (
            task_id     TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,          -- 'shell' | 'agent' | 'workflow'
            session_id  TEXT,                   -- owning chat session ('' = orphan)
            title       TEXT NOT NULL,
            command     TEXT,                   -- shell kind only
            status      TEXT NOT NULL,          -- running|completed|failed|stopped|interrupted
            exit_code   INTEGER,                -- NULL when unknown (re-adopted orphans)
            pid         INTEGER,                -- shell: process-group leader
            output_path TEXT,
            result_text TEXT,
            meta        TEXT,                   -- JSON blob for kind-specific fields
            owner_pid   INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(session_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status)",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
