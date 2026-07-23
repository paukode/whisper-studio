"""Purge orphaned session_costs/tasks rows left by incomplete deletes.

Earlier the delete endpoint only removed the parent ``sessions`` row, so
child rows in ``session_costs`` and ``tasks`` were orphaned. This one-time
sweep removes any child rows whose ``session_id`` no longer exists in
``sessions``. The delete endpoint now cascades, so no new orphans accrue.
"""

VERSION = 4
DESCRIPTION = "Purge orphaned session_costs/tasks rows left by incomplete deletes"


def migrate(conn):
    # ``tasks`` is created lazily by tasks_tracker, not by a migration, so it
    # may not exist yet when the runner fires during lifespan init. Guard on
    # table presence before deleting from each.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "session_costs" in tables:
        conn.execute("DELETE FROM session_costs WHERE session_id NOT IN (SELECT id FROM sessions)")
    if "tasks" in tables:
        conn.execute("DELETE FROM tasks WHERE session_id NOT IN (SELECT id FROM sessions)")
