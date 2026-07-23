"""Workflow run registry (WS-D ultracode runtime).

One row per workflow run: the model-authored orchestration script's lifecycle,
caps, and spend ledger. The journal (per-call detail) lives in
data_root()/workflows/runs/<run_id>/journal.jsonl — this table is the queryable
index with the running-lease used by boot reconcile (rows still 'running' at
startup are flipped to 'stale', mirroring cron_history).
"""

import sqlite3

VERSION = 9
DESCRIPTION = "Add workflow_runs table for the ultracode workflow runtime"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id          TEXT PRIMARY KEY,
            name            TEXT NOT NULL DEFAULT '',
            session_id      TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'done', 'failed', 'stopped', 'stale')),
            phases_json     TEXT NOT NULL DEFAULT '[]',
            args_json       TEXT NOT NULL DEFAULT 'null',
            script_hash     TEXT NOT NULL DEFAULT '',
            model_key       TEXT NOT NULL DEFAULT '',
            agents_spawned  INTEGER NOT NULL DEFAULT 0,
            tokens_in       INTEGER NOT NULL DEFAULT 0,
            tokens_out      INTEGER NOT NULL DEFAULT 0,
            cost_usd        REAL NOT NULL DEFAULT 0,
            budget_usd      REAL,
            cap_reached     INTEGER NOT NULL DEFAULT 0,
            error           TEXT NOT NULL DEFAULT '',
            result_json     TEXT NOT NULL DEFAULT 'null',
            resumed_from    TEXT NOT NULL DEFAULT '',
            started_at      TEXT NOT NULL,
            finished_at     TEXT
        )
        """
    )
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_session ON workflow_runs(session_id, started_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status)",
    ):
        conn.execute(stmt)
