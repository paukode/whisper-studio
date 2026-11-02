"""Budget downgrade: a cheaper model when a cost limit is near.

When ``model_fallback_enabled`` is on and the session or daily spend reaches
BUDGET_THRESHOLD_PCT of its limit, a turn runs on the next cheaper model.
"Next cheaper" is computed from the authoritative per-model price list
(``server.costs.tracker._MODEL_PRICING``), so it tracks the catalog.
"""

import logging

from server.costs.tracker import _MODEL_PRICING, CostLogUnreadable, get_session_usage
from server.costs.usage import today_spend_usd
from server.infrastructure.config import load_config

log = logging.getLogger("whisper-studio")

# Budget threshold: switch to cheaper model when this % of limit is used
BUDGET_THRESHOLD_PCT = 0.80


def _model_cost(model_key: str) -> float | None:
    """Combined list price (input + output, USD/1M tok) for ranking, or None
    if the model is not in the price table."""
    pricing = _MODEL_PRICING.get(model_key)
    if not pricing:
        return None
    return float(pricing.get("input", 0.0)) + float(pricing.get("output", 0.0))


def get_next_fallback(current_model_key: str) -> str | None:
    """Return the next-cheaper model key for ``current_model_key``.

    "Next cheaper" is the most expensive model whose list price is *strictly*
    below the current model's: the smallest capability drop that still relieves
    budget pressure. Ranked from ``_MODEL_PRICING`` so it never goes stale.

    Robust by construction:
      - An unknown current key (not priced) returns None: we can't rank it, so
        we don't guess a downgrade (and unpriced models bill $0 anyway, so
        "downgrading" them to a priced model would only *raise* cost).
      - The cheapest model returns None (nothing cheaper).
      - Never raises on an unexpected key.
    """
    current_cost = _model_cost(current_model_key)
    if current_cost is None:
        return None
    cheaper = [
        (key, cost)
        for key in _MODEL_PRICING
        if (cost := _model_cost(key)) is not None and cost < current_cost
    ]
    if not cheaper:
        return None
    # Most expensive among the strictly-cheaper set = the next step down.
    cheaper.sort(key=lambda kv: (-kv[1], kv[0]))
    return cheaper[0][0]


def should_downgrade_for_budget(
    session_id: str,
    model_key: str,
) -> str | None:
    """Check if we should downgrade to a cheaper model due to budget pressure.

    Returns the recommended model key, or None if no downgrade needed.
    """
    config = load_config()
    if not config.get("model_fallback_enabled", False):
        return None

    session_limit = config.get("max_session_cost_usd", 0.0)
    daily_limit = config.get("max_daily_cost_usd", 0.0)
    try:
        return _budget_downgrade(session_id, model_key, session_limit, daily_limit)
    except CostLogUnreadable as e:
        # The budget check itself stops the run with this reason; a
        # downgrade cannot be judged without the spend either.
        log.warning("No budget downgrade for %s: %s", model_key, e)
        return None


def _budget_downgrade(
    session_id: str, model_key: str, session_limit: float, daily_limit: float
) -> str | None:
    # Check session budget pressure
    if session_limit > 0:
        session_cost = get_session_usage(session_id)["cost_usd"]
        if session_cost >= session_limit * BUDGET_THRESHOLD_PCT:
            fallback = get_next_fallback(model_key)
            if fallback:
                log.info(
                    "Budget pressure: session cost $%.4f (%.0f%% of $%.2f): downgrading %s to %s",
                    session_cost,
                    session_cost / session_limit * 100,
                    session_limit,
                    model_key,
                    fallback,
                )
                return fallback

    # Check daily budget pressure
    if daily_limit > 0:
        today_cost = today_spend_usd()
        if today_cost >= daily_limit * BUDGET_THRESHOLD_PCT:
            fallback = get_next_fallback(model_key)
            if fallback:
                log.info(
                    "Budget pressure: today's cost (UTC day) $%.4f (%.0f%% of $%.2f): downgrading %s to %s",
                    today_cost,
                    today_cost / daily_limit * 100,
                    daily_limit,
                    model_key,
                    fallback,
                )
                return fallback

    return None


def resolve_model_with_fallback(
    model_key: str,
    chat_models: dict,
    session_id: str = "",
) -> tuple[str, str]:
    """Resolve a model key the caller already checked is in ``chat_models``
    to (model_key, model_id), applying the budget downgrade when enabled.
    The key may differ from the input when a downgrade applies."""
    config = load_config()
    if config.get("model_fallback_enabled", False) and session_id:
        downgrade = should_downgrade_for_budget(session_id, model_key)
        # The recommended next-cheaper key may not exist in this deployment's
        # chat_models. Walk further down the (strictly cheaper) chain until we
        # hit one that IS configured, rather than silently skipping the
        # downgrade and leaving the budget unprotected. Terminates because each
        # step is strictly cheaper and the chain is finite (ends at None).
        while downgrade and downgrade not in chat_models:
            downgrade = get_next_fallback(downgrade)
        if downgrade:
            model_key = downgrade
    return model_key, chat_models[model_key]
