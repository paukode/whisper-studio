"""Read the cost log the way the tests check it: raw rows, oldest first."""

from server.costs import tracker


def session_cost_rows(session_id: str) -> list[dict]:
    """Every recorded call of one session, oldest first (raw token rows)."""
    with tracker._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM session_costs WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]
