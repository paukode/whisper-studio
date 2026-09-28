"""Cost rows stop storing a dollar figure: every reader prices them when read.

``cost_usd`` froze the rate table of the day a row was written, so a rate
correction never reached history, and a reader that summed it disagreed with
one that priced the tokens. Rows now hold token counts and their provenance
only; server.costs.tracker.estimate_cost prices them with the current rates
wherever they are read (the Costs tab, the export, the composer readout, the
budget caps). The rollup backup table keeps its column untouched.
"""

import sqlite3

VERSION = 22
DESCRIPTION = "Drop session_costs.cost_usd; cost is priced from token counts at read time"


def migrate(conn: sqlite3.Connection) -> None:
    columns = [r[1] for r in conn.execute("PRAGMA table_info(session_costs)").fetchall()]
    if "cost_usd" in columns:
        conn.execute("ALTER TABLE session_costs DROP COLUMN cost_usd")
