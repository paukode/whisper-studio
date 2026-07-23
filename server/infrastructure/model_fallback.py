"""Model fallback chain — degrades from expensive to cheaper models.

Fallback triggers:
  1. Model unavailable (AccessDeniedException, model not found)
  2. Over budget (session or daily cost limit approaching)

The chain is DERIVED from the authoritative per-model price list
(``server.costs.tracker._MODEL_PRICING``) rather than hardcoded, so it can't
drift out of sync with the model catalog: every priced model participates and
"next cheaper" is computed from list price on the fly. A hardcoded chain is
kept only as a fallback for the (unexpected) case where the pricing table is
unavailable.
"""

import logging

from server.costs.tracker import get_session_summary, get_today_total_cost
from server.infrastructure.config import load_config

log = logging.getLogger("whisper-studio")

# Authoritative price table. Imported lazily-safe: if tracker's private dict is
# ever renamed/removed we degrade to the static chain below instead of crashing
# the module at import time.
try:
    from server.costs.tracker import _MODEL_PRICING
except Exception:  # pragma: no cover - defensive
    _MODEL_PRICING = {}

# Static fallback order (most capable → least capable), used only when the
# pricing table is empty/unavailable. Kept in real catalog order so the module
# still works standalone. Two provider chains flattened by capability.
_STATIC_FALLBACK_CHAIN = [
    "fable5.0",
    "opus4.8",
    "opus4.7",
    "opus4.6",
    "gpt5.6-sol",
    "gpt5.5",
    "gpt5.6-terra",
    "gpt5.4",
    "sonnet5",
    "sonnet",
    "gpt5.6-luna",
    "haiku",
]

# Budget threshold: switch to cheaper model when this % of limit is used
BUDGET_THRESHOLD_PCT = 0.80


def _model_cost(model_key: str) -> float | None:
    """Combined list price (input + output, USD/1M tok) for ranking, or None
    if the model is not in the price table."""
    pricing = _MODEL_PRICING.get(model_key)
    if not pricing:
        return None
    return float(pricing.get("input", 0.0)) + float(pricing.get("output", 0.0))


def _derive_chain() -> list[str]:
    """Catalog ordered most-expensive → cheapest (stable tiebreak on key).

    Derived from the price table so it tracks the live catalog. Falls back to
    the static chain when no pricing is available."""
    if not _MODEL_PRICING:
        return list(_STATIC_FALLBACK_CHAIN)
    return sorted(_MODEL_PRICING, key=lambda k: (-(_model_cost(k) or 0.0), k))


# Most capable → least capable. Consumed by resolve_model_with_fallback to pick
# the most capable model that is actually present in chat_models.
FALLBACK_CHAIN = _derive_chain()


def get_next_fallback(current_model_key: str) -> str | None:
    """Return the next-cheaper model key for ``current_model_key``.

    "Next cheaper" is the most expensive model whose list price is *strictly*
    below the current model's — the smallest capability drop that still relieves
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

    # Check session budget pressure
    if session_limit > 0:
        summary = get_session_summary(session_id)
        session_cost = summary.get("total_cost_usd", 0.0)
        if session_cost >= session_limit * BUDGET_THRESHOLD_PCT:
            fallback = get_next_fallback(model_key)
            if fallback:
                log.info(
                    "Budget pressure: session cost $%.4f (%.0f%% of $%.2f) — downgrading %s → %s",
                    session_cost,
                    session_cost / session_limit * 100,
                    session_limit,
                    model_key,
                    fallback,
                )
                return fallback

    # Check daily budget pressure
    if daily_limit > 0:
        today_cost = get_today_total_cost()
        if today_cost >= daily_limit * BUDGET_THRESHOLD_PCT:
            fallback = get_next_fallback(model_key)
            if fallback:
                log.info(
                    "Budget pressure: daily cost $%.4f (%.0f%% of $%.2f) — downgrading %s → %s",
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
    """Resolve model key to model ID, applying fallback if needed.

    Returns (model_key, model_id) — the model_key may differ from input
    if a fallback was applied.
    """
    config = load_config()
    if not config.get("model_fallback_enabled", False):
        model_id = chat_models.get(model_key) or next(iter(chat_models.values()))
        return model_key, model_id

    # Budget-based downgrade
    if session_id:
        downgrade = should_downgrade_for_budget(session_id, model_key)
        # The recommended next-cheaper key may not exist in this deployment's
        # chat_models. Walk further down the (strictly cheaper) chain until we
        # hit one that IS configured, rather than silently skipping the
        # downgrade and leaving the budget unprotected. Terminates because each
        # step is strictly cheaper and the chain is finite (ends at None).
        while downgrade and downgrade not in chat_models:
            downgrade = get_next_fallback(downgrade)
        if downgrade and downgrade in chat_models:
            model_key = downgrade

    model_id = chat_models.get(model_key)
    if not model_id:
        # Model key not in chat_models — try fallback chain
        for fallback_key in FALLBACK_CHAIN:
            if fallback_key in chat_models:
                model_key = fallback_key
                model_id = chat_models[fallback_key]
                break

    if not model_id:
        model_id = next(iter(chat_models.values()))

    return model_key, model_id
