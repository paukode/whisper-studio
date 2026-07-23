"""Per-model pricing: explicit version-keyed rates, no fallback bucket, and the
two provider cache conventions (Anthropic disjoint vs OpenAI cached-in-input).

All deterministic — no Bedrock needed.
"""

import pytest

from server.chat import _get_chat_models
from server.costs.tracker import estimate_cost, get_model_pricing


def test_every_cloud_model_is_priced():
    # With no fallback, an unpriced model silently bills $0 — so every billable
    # (cloud / Bedrock) model the UI can select MUST have its own pricing entry.
    # Local "local:" models are on-device and free; they never hit estimate_cost.
    for key, model_id in _get_chat_models().items():
        if str(model_id).startswith("local:"):
            continue
        assert get_model_pricing(key) is not None, f"no pricing entry for {key!r}"


def test_no_fallback_for_unknown_or_legacy_key():
    # The generic "opus" key was renamed to opus4.6 and must not resolve.
    assert get_model_pricing("opus") is None
    assert get_model_pricing("totally-unknown") is None
    # Unknown keys bill $0 rather than inheriting another model's rate.
    assert estimate_cost("totally-unknown", 1_000_000, 500_000) == 0.0


def test_versioned_opus_keys_bill_at_own_rate():
    # Regression: opus4.7/opus4.8 used to fall through to a $15/$75 "opus"
    # bucket. They must now bill at the real Opus rate of $5/$25.
    for key in ("opus4.6", "opus4.7", "opus4.8"):
        cost = estimate_cost(key, 2_160_997, 85_039)
        assert cost == pytest.approx(2_160_997 / 1e6 * 5 + 85_039 / 1e6 * 25)
        assert cost == pytest.approx(12.931, abs=1e-3)  # not the old $39.67


def test_gpt_cached_input_not_double_counted():
    # OpenAI input_tokens INCLUDES cached input. With cached_in_input=True the
    # cached portion is billed once at cache_read, never also at the input rate.
    full = estimate_cost("gpt5.5", 1_000_000, 0)
    partial = estimate_cost("gpt5.5", 1_000_000, 0, cache_read_tokens=400_000, cached_in_input=True)
    # 600k @ $5.50 + 400k @ $0.55
    assert partial == pytest.approx(0.6 * 5.50 + 0.4 * 0.55)
    assert partial < full  # caching is a discount, not a surcharge


def test_anthropic_cache_buckets_are_additive():
    # Anthropic (cached_in_input=False): input, cache_read and cache_creation
    # are disjoint, so each adds on top — caching does not shrink input.
    base = estimate_cost("sonnet", 1_000_000, 0)
    with_read = estimate_cost("sonnet", 1_000_000, 0, cache_read_tokens=500_000)
    assert with_read == pytest.approx(base + 500_000 / 1e6 * 0.30)
