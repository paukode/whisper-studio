"""Cost budget enforcement — per-session and daily cost limits.

Checks are performed before each Bedrock API call. When a limit is exceeded,
the caller receives a warning or hard stop depending on configuration.

Budget settings in config.json:
    max_session_cost_usd: 0.0    (0 = unlimited)
    max_daily_cost_usd: 0.0      (0 = unlimited)
"""

import logging

from server.costs.tracker import get_session_summary, get_today_total_cost
from server.infrastructure.config import load_config

log = logging.getLogger("whisper-studio")


class BudgetExceeded:
    """Result of a budget check when a limit is hit."""

    def __init__(self, kind: str, limit: float, current: float, message: str):
        self.kind = kind  # "session" or "daily"
        self.limit = limit
        self.current = current
        self.message = message


def check_budget(session_id: str) -> BudgetExceeded | None:
    """Check if the session or daily cost budget has been exceeded.

    Returns None if within budget, or a BudgetExceeded with details.
    """
    config = load_config()

    # Session budget
    session_limit = config.get("max_session_cost_usd", 0.0)
    if session_limit > 0:
        summary = get_session_summary(session_id)
        session_cost = summary.get("total_cost_usd", 0.0)
        if session_cost >= session_limit:
            return BudgetExceeded(
                kind="session",
                limit=session_limit,
                current=session_cost,
                message=(
                    f"Session cost ${session_cost:.4f} has reached the limit of "
                    f"${session_limit:.2f}. Start a new session or increase "
                    f"max_session_cost_usd in settings."
                ),
            )

    # Daily budget
    daily_limit = config.get("max_daily_cost_usd", 0.0)
    if daily_limit > 0:
        today_cost = get_today_total_cost()
        if today_cost >= daily_limit:
            return BudgetExceeded(
                kind="daily",
                limit=daily_limit,
                current=today_cost,
                message=(
                    f"Daily cost ${today_cost:.4f} has reached the limit of "
                    f"${daily_limit:.2f}. Increase max_daily_cost_usd in settings "
                    f"or wait until tomorrow."
                ),
            )

    return None
