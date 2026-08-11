"""Workflow budgets are token-denominated now (output tokens, default 600k).

The USD column is dropped rather than converted: a dollar cap has no faithful
token equivalent after the fact, and it only ever applied to already-finished
runs. Migration 009's CREATE now includes ``budget_tokens`` directly (it also
serves as the lazy bootstrapper in server/workflows/manager.py), so this
migration only rewrites tables created before the switch and is a no-op on
fresh databases.
"""

import sqlite3

VERSION = 13
DESCRIPTION = "workflow_runs: replace budget_usd with budget_tokens"


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_runs)")}
    if not cols:
        return  # table not created yet; 009 creates the new shape
    if "budget_tokens" not in cols:
        conn.execute("ALTER TABLE workflow_runs ADD COLUMN budget_tokens INTEGER")
    if "budget_usd" in cols:
        conn.execute("ALTER TABLE workflow_runs DROP COLUMN budget_usd")
