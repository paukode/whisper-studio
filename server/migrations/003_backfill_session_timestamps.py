"""Backfill empty updated_at / created_at on existing session rows.

Earlier versions of the React client sent `updatedAt` in the save payload, but
the backend looked up `body.get("date", "")` and stored an empty string. As a
result, every existing row has `updated_at = ''`, the `ORDER BY updated_at
DESC` list query returns rows in undefined order, and `formatSessionTime("")`
in the sidebar renders no timestamp.

This migration repairs the data:
  - Rows with empty `updated_at` are filled from `created_at` (best signal we
    have for a row's age).
  - Rows where both are empty get the migration's run-time `now` so they at
    least have *some* sortable value.
"""

from datetime import datetime, timezone

VERSION = 3
DESCRIPTION = "Backfill empty session updated_at/created_at timestamps"


def migrate(conn):
    now = datetime.now(timezone.utc).isoformat()
    # If updated_at is empty but created_at is set, copy created_at over.
    conn.execute(
        "UPDATE sessions SET updated_at = created_at "
        "WHERE (updated_at IS NULL OR updated_at = '') "
        "AND (created_at IS NOT NULL AND created_at <> '')"
    )
    # If both are still empty, stamp them with `now` so the row is sortable
    # and the sidebar renders a (recent) time rather than blank.
    conn.execute(
        "UPDATE sessions SET updated_at = ?, created_at = ? "
        "WHERE (updated_at IS NULL OR updated_at = '') "
        "AND (created_at IS NULL OR created_at = '')",
        (now, now),
    )
