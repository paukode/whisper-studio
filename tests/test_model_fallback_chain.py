"""The budget-downgrade chain must track the live model catalog: "next
cheaper" is ranked from the price list, so every priced model takes part and a
stale hardcoded order can never make downgrading a silent no-op."""

from server.costs.tracker import _MODEL_PRICING
from server.infrastructure import model_fallback as mf


def _cost(key: str) -> float:
    p = _MODEL_PRICING[key]
    return float(p["input"]) + float(p["output"])


def test_default_model_downgrades_to_cheaper_key():
    # A flagship (opus5.5) must yield a genuinely cheaper, valid key rather
    # than None/ValueError.
    nxt = mf.get_next_fallback("opus5.5")
    assert nxt is not None
    assert nxt in _MODEL_PRICING
    assert _cost(nxt) < _cost("opus5.5")


def test_every_priced_model_except_cheapest_has_a_fallback():
    # The lowest price can be shared: one model priced under two ids (Cohere
    # Embed v4's in-region id and its global profile). Every key at that
    # price has nothing strictly cheaper; every other key steps down.
    floor = min(_cost(key) for key in _MODEL_PRICING)
    for key in _MODEL_PRICING:
        nxt = mf.get_next_fallback(key)
        if _cost(key) == floor:
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


def test_resolve_downgrades_only_under_budget_pressure(monkeypatch):
    models = {"opus5.5": "id-opus", "haiku": "id-haiku"}
    monkeypatch.setattr(mf, "load_config", lambda: {"model_fallback_enabled": False})
    assert mf.resolve_model_with_fallback("opus5.5", models, "s1") == ("opus5.5", "id-opus")

    monkeypatch.setattr(mf, "load_config", lambda: {"model_fallback_enabled": True})
    monkeypatch.setattr(mf, "should_downgrade_for_budget", lambda sid, key: "sonnet5.5")
    # sonnet5.5 is not configured here: the walk continues down to a configured key.
    key, model_id = mf.resolve_model_with_fallback("opus5.5", models, "s1")
    assert (key, model_id) == ("haiku", "id-haiku")
