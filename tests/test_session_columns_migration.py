"""Migration 023 drops the two sessions columns nothing ever wrote
(compaction_count, latched_config) and keeps every row's real data."""

import importlib
import sqlite3

from server.infrastructure import sessions

drop = importlib.import_module("server.migrations.023_drop_unused_session_columns")

KEPT = "SELECT id, title, created_at, updated_at, chat_history, workspace_path FROM sessions"


def _columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}


def _as_migration_002_left_it(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "chat_history TEXT NOT NULL DEFAULT '[]', workspace_path TEXT DEFAULT '', "
        "compaction_count INTEGER DEFAULT 0, latched_config TEXT DEFAULT '{}')"
    )
    conn.execute("CREATE INDEX idx_sessions_updated ON sessions(updated_at DESC)")
    conn.executemany(
        "INSERT INTO sessions (id, title, created_at, updated_at, chat_history, workspace_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("s1", "Split the parser", "c1", "u1", '[{"role": "user"}]', "/Users/me/parser"),
            ("s2", "Weekly sync", "c2", "u2", "[]", ""),
        ],
    )
    conn.commit()
    return conn


def test_drops_both_columns_and_keeps_every_row(tmp_path):
    conn = _as_migration_002_left_it(tmp_path / "sessions.db")
    before = conn.execute(KEPT).fetchall()

    drop.migrate(conn)

    assert {"compaction_count", "latched_config"}.isdisjoint(_columns(conn))
    assert conn.execute(KEPT).fetchall() == before


def test_a_rerun_is_a_no_op(tmp_path):
    conn = _as_migration_002_left_it(tmp_path / "sessions.db")
    drop.migrate(conn)
    columns = _columns(conn)

    drop.migrate(conn)

    assert _columns(conn) == columns


def test_the_database_the_app_runs_on_has_neither_column():
    """Every test gets a copy of the template built by _ensure_db plus every
    migration in order, which is the schema a real install ends up with."""
    with sessions._get_conn() as conn:
        present = _columns(conn)
    assert "workspace_path" in present
    assert {"compaction_count", "latched_config"}.isdisjoint(present)
