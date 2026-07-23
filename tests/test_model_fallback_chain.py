"""The budget-downgrade fallback chain must track the live model catalog.

Regression: FALLBACK_CHAIN used to be a stale hardcoded list (["opus4.6",
"sonnet", "haiku"]) while the catalog default moved to opus4.8 and gained
opus4.7 / sonnet5 / fable5.0 / gpt5.5 / gpt5.4. get_next_fallback did
FALLBACK_CHAIN.index(current), which raised ValueError for every key not in
the stale list and returned None — making budget downgrading a silent no-op
for the default model and nearly every other model.
"""

from server.costs.tracker import _MODEL_PRICING
from server.infrastructure import model_fallback as mf


def _cost(key: str) -> float:
    p = _MODEL_PRICING[key]
    return float(p["input"]) + float(p["output"])


def test_default_model_downgrades_to_cheaper_key():
    # The catalog default (opus4.8) must yield a genuinely cheaper, valid key
    # rather than None/ValueError.
    nxt = mf.get_next_fallback("opus4.8")
    assert nxt is not None
    assert nxt in _MODEL_PRICING
    assert _cost(nxt) < _cost("opus4.8")


def test_every_priced_model_except_cheapest_has_a_fallback():
    cheapest = min(_MODEL_PRICING, key=_cost)
    for key in _MODEL_PRICING:
        nxt = mf.get_next_fallback(key)
        if key == cheapest:
            assert nxt is None
        else:
            assert nxt is not None
            assert _cost(nxt) < _cost(key)


def test_cheapest_model_has_no_fallback():
    cheapest = min(_MODEL_PRICING, key=_cost)
    assert mf.get_next_fallback(cheapest) is None


def test_unknown_key_does_not_raise():
    # An unknown / unpriced key must degrade gracefully, never raise.
    assert mf.get_next_fallback("totally-made-up-model") is None
    assert mf.get_next_fallback("") is None


def test_fallback_chain_covers_catalog_keys():
    # The chain used to walk chat_models is derived from pricing, so it must
    # contain the current catalog keys (not the stale opus4.6-only list).
    for key in ("opus4.8", "sonnet", "haiku"):
        assert key in mf.FALLBACK_CHAIN
    # Ordered most-expensive first so resolve_model_with_fallback prefers the
    # most capable available model.
    costs = [_cost(k) for k in mf.FALLBACK_CHAIN if k in _MODEL_PRICING]
    assert costs == sorted(costs, reverse=True)
