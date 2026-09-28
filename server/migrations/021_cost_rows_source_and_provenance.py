"""Cost rows gain a source and count provenance; agent rollups leave the log.

Each session_costs row now says where the call came from (``source``: chat,
agent, workflow, compaction, title, ...) and where its token counts came from
(``count_source`` reported or estimated, ``estimated_fields`` naming the
estimated ones, ``count_detail`` holding both reported sources when they
disagree).

Since the agent runtime moved onto the shared turn engine (v2.0.0) every agent
round was already recorded under the parent session, and spawn_agent, team
members and the /subagent stream then wrote the run's cumulative totals again
as a '<key>_agent' or '<key>_subagent' row. Where a session's own rounds of
the base model cover the rollups' input tokens the rollups are duplicates:
they move to session_costs_rollup_backup (kept, never read) so history stops
counting agent work twice. A rollup with no such coverage is the only record
of its spend (a run from before the engine migration): it stays, relabelled to
the base model key with source 'agent' so it prices under the right rates.

Rows written before this migration get the best source the row itself shows:
review-fork rounds (turn 900 and up) and dream consolidation are memory, the
old per-run voice ids are voice, the rest is unattributed. A row with no
input and no cache tokens but some output could only come from an estimate
(the GPT early release or a local server without usage), so it is marked
estimated.
"""

import sqlite3

VERSION = 21
DESCRIPTION = "Cost rows gain source and count provenance; duplicate agent rollups move to a backup"

BACKUP_TABLE = "session_costs_rollup_backup"
REVIEW_TURN_BASE = 900  # server/memory/review_fork.py

_ROLLUP_SUFFIXES = ("_subagent", "_agent")

_NEW_COLUMNS = (
    ("source", "TEXT NOT NULL DEFAULT ''"),
    ("count_source", "TEXT NOT NULL DEFAULT 'reported'"),
    ("estimated_fields", "TEXT NOT NULL DEFAULT ''"),
    ("count_detail", "TEXT NOT NULL DEFAULT ''"),
)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _base_key(model: str) -> str:
    for suffix in _ROLLUP_SUFFIXES:
        if model.endswith(suffix):
            return model[: -len(suffix)]
    return model


def _move_rollups(conn: sqlite3.Connection) -> None:
    rollups = conn.execute(
        "SELECT id, session_id, model, input_tokens FROM session_costs "
        "WHERE model LIKE '%\\_agent' ESCAPE '\\' OR model LIKE '%\\_subagent' ESCAPE '\\'"
    ).fetchall()
    if not rollups:
        return
    groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row_id, session_id, model, input_tokens in rollups:
        groups.setdefault((session_id, _base_key(model)), []).append((row_id, input_tokens or 0))

    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} AS SELECT * FROM session_costs WHERE 0"
    )
    for (session_id, base), rows in groups.items():
        ids = [row_id for row_id, _ in rows]
        marks = ",".join("?" for _ in ids)
        covered_by = conn.execute(
            "SELECT COALESCE(SUM(input_tokens), 0) FROM session_costs "
            "WHERE session_id = ? AND model = ?",
            (session_id, base),
        ).fetchone()[0]
        if covered_by >= sum(tokens for _, tokens in rows):
            conn.execute(
                f"INSERT INTO {BACKUP_TABLE} SELECT * FROM session_costs WHERE id IN ({marks})",
                ids,
            )
            conn.execute(f"DELETE FROM session_costs WHERE id IN ({marks})", ids)
        else:
            conn.execute(
                f"UPDATE session_costs SET model = ?, source = 'agent' WHERE id IN ({marks})",
                [base, *ids],
            )


def migrate(conn: sqlite3.Connection) -> None:
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "session_costs" not in tables:
        return
    existing = _columns(conn, "session_costs")
    if "source" in existing:
        # Created in the current shape (server.costs.tracker's bootstrap):
        # there are no old rows to backfill.
        return
    # One transaction for the DDL and the data moves, so a failure part way
    # leaves the table as it was and the next start retries the whole step.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    for name, decl in _NEW_COLUMNS:
        conn.execute(f"ALTER TABLE session_costs ADD COLUMN {name} {decl}")
    conn.execute(
        """
        UPDATE session_costs SET source = CASE
            WHEN turn_number >= ? THEN 'memory'
            WHEN session_id = 'dream' THEN 'memory'
            WHEN session_id LIKE 'voice-%' THEN 'voice'
            ELSE 'unattributed'
        END
        """,
        (REVIEW_TURN_BASE,),
    )
    conn.execute(
        "UPDATE session_costs SET count_source = 'estimated', estimated_fields = 'input,output' "
        "WHERE input_tokens = 0 AND cache_read_tokens = 0 AND cache_creation_tokens = 0 "
        "AND output_tokens > 0"
    )
    _move_rollups(conn)
