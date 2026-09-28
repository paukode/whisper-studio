"""Cost budget enforcement: per-session and daily cost limits.

Checks are performed before each Bedrock API call. When a limit is exceeded,
the caller receives a warning or hard stop depending on configuration.

Budget settings in config.json:
    max_session_cost_usd: 0.0    (0 = unlimited)
    max_daily_cost_usd: 0.0      (0 = unlimited; the day is the UTC day)

Both caps price the recorded token counts with the current rate table
(server.costs.tracker.estimate_cost), the same figure the Costs tab shows. A
cap whose spend cannot be read is treated as reached, with the reason.
"""

import logging

from server.costs.tracker import CostLogUnreadable, get_session_usage
from server.costs.usage import today_spend_usd
from server.infrastructure.config import load_config

log = logging.getLogger("whisper-studio")


class BudgetExceeded:
    """Result of a budget check when a limit is hit."""

    def __init__(self, kind: str, limit: float, current: float, message: str):
        self.kind = kind  # "session" or "daily"
        self.limit = limit
        self.current = current
        self.message = message


def _unreadable(kind: str, limit: float, error: Exception, key: str) -> BudgetExceeded:
    """A cap whose spend cannot be read stops the run like a reached cap,
    with the reason: reading the spend as $0 would let it run past the cap
    unnoticed."""
    what = "session" if kind == "session" else "daily (UTC day)"
    return BudgetExceeded(
        kind=kind,
        limit=limit,
        current=0.0,
        message=(
            f"{error}. The {what} cap of ${limit:.2f} cannot be checked, so the run "
            f"stops here. Fix the cost log, or set {key} to 0 in settings to run "
            "without this cap."
        ),
    )


def check_budget(session_id: str) -> BudgetExceeded | None:
    """Check if the session or daily cost budget has been exceeded.

    Returns None if within budget, or a BudgetExceeded with details.
    """
    return _check(session_id, 1.0)


def check_budget_soft(session_id: str, fraction: float = 0.9) -> BudgetExceeded | None:
    """The soft limit: ``fraction`` of a configured cap has been spent. The
    turn engine starts its final (reporting) round here so the report is
    written inside the budget instead of after the cap kills the run."""
    return _check(session_id, max(0.0, min(1.0, fraction)))


def _check(session_id: str, fraction: float) -> BudgetExceeded | None:
    config = load_config()
    soft = fraction < 1.0
    pct = f"{int(round(fraction * 100))} percent of"

    # Session budget
    session_limit = config.get("max_session_cost_usd", 0.0)
    if session_limit > 0:
        try:
            session_cost = get_session_usage(session_id)["cost_usd"]
        except CostLogUnreadable as e:
            return _unreadable("session", session_limit, e, "max_session_cost_usd")
        if session_cost >= session_limit * fraction:
            return BudgetExceeded(
                kind="session",
                limit=session_limit,
                current=session_cost,
                message=(
                    f"Session cost ${session_cost:.4f} has reached {pct + ' ' if soft else ''}"
                    f"the limit of ${session_limit:.2f}. Start a new session or increase "
                    f"max_session_cost_usd in settings."
                ),
            )

    # Daily budget (the UTC day, the same day AWS Cost Explorer reports)
    daily_limit = config.get("max_daily_cost_usd", 0.0)
    if daily_limit > 0:
        try:
            today_cost = today_spend_usd()
        except CostLogUnreadable as e:
            return _unreadable("daily", daily_limit, e, "max_daily_cost_usd")
        if today_cost >= daily_limit * fraction:
            return BudgetExceeded(
                kind="daily",
                limit=daily_limit,
                current=today_cost,
                message=(
                    f"Today's cost (UTC day) ${today_cost:.4f} has reached "
                    f"{pct + ' ' if soft else ''}the limit of ${daily_limit:.2f}. Increase "
                    f"max_daily_cost_usd in settings or wait until the next UTC day "
                    f"starts (00:00 UTC)."
                ),
            )

    return None
