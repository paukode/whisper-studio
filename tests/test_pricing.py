"""Per-model pricing: explicit version-keyed rates, no fallback bucket, and the
two provider cache conventions (Anthropic disjoint vs OpenAI cached-in-input).

Rates are sourced from pricing.example.json (committed defaults) with an
optional gitignored pricing.json overlaying them per key.

All deterministic — no Bedrock needed.
"""

import json

import pytest

from server.chat import _get_chat_models
from server.costs import tracker
from server.costs.tracker import estimate_cost, get_model_pricing


def test_every_cloud_model_is_priced():
    # With no fallback, an unpriced model silently bills $0 — so every billable
    # (cloud / Bedrock) model the UI can select MUST have its own pricing entry.
    # Local "local:" models are on-device and free; they never hit estimate_cost.
    for key, model_id in _get_chat_models().items():
        if str(model_id).startswith("local:"):
            continue
        assert get_model_pricing(key) is not None, f"no pricing entry for {key!r}"


def test_the_voice_and_index_cost_rows_are_priced():
    # Nova Sonic logs speech and text under their own keys and Cohere Embed
    # under its model id; each has a Price List rate, so none of these billed
    # calls shows as $0.
    from server.index.config import COHERE_EMBED_MODEL_ID
    from server.voice.cost_log import model_keys
    from server.voice.protocol import DEFAULT_MODEL_ID

    for key in (*model_keys(DEFAULT_MODEL_ID).values(), COHERE_EMBED_MODEL_ID):
        rates = get_model_pricing(key)
        assert rates is not None, key
        assert rates["input"] > 0, key
        assert estimate_cost(key, 1_000_000, 0) == pytest.approx(rates["input"]), key
    speech, text = (get_model_pricing(k) for k in model_keys(DEFAULT_MODEL_ID).values())
    assert speech["input"] > text["input"] and speech["output"] > text["output"]


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
    # OpenAI input_tokens INCLUDES cached input. The pricing entry's
    # cached_in_input flag bills the cached portion once at cache_read, never
    # also at the input rate, and no caller has to remember to pass it (the
    # agent rollup, the agent budget bar and the workflow ledger never did).
    rates = get_model_pricing("gpt5.5")
    assert rates["cached_in_input"] is True
    full = estimate_cost("gpt5.5", 1_000_000, 0)
    partial = estimate_cost("gpt5.5", 1_000_000, 0, cache_read_tokens=400_000)
    assert partial == pytest.approx(0.6 * rates["input"] + 0.4 * rates["cache_read"])
    assert partial < full  # caching is a discount, not a surcharge


def test_gpt_cache_writes_are_billed_once_inside_input():
    # Mantle's cache_write_tokens, like cached_tokens, is part of
    # input_tokens: the written share is billed at cache_write instead of
    # input, never at both, and the prompt is input_tokens itself.
    rates = get_model_pricing("gpt6-astra")
    inp, read, write = 1_000_000, 600_000, 380_000
    cost = estimate_cost("gpt6-astra", inp, 0, read, write)
    assert cost == pytest.approx(
        0.02 * rates["input"] + 0.6 * rates["cache_read"] + 0.38 * rates["cache_write"]
    )
    assert cost > estimate_cost("gpt6-astra", inp, 0, read, 0)  # the write premium
    assert tracker.prompt_tokens_for("gpt6-astra", inp, read, write) == inp
    assert tracker.prompt_tokens_for("gpt6-astra", inp, read, 0) == inp


def test_a_gpt_model_with_no_cache_write_price_bills_a_write_as_input():
    # The GPT-5.5 and 5.4 cards price no cache write: a reported write costs
    # what plain input does, never nothing.
    for key in ("gpt5.5", "gpt5.4"):
        assert estimate_cost(key, 1_000_000, 0, 0, 400_000) == pytest.approx(
            estimate_cost(key, 1_000_000, 0)
        ), key


def test_the_agent_budget_bar_shows_the_engines_cost():
    # The engine prices every round at its own tier (call_cost); the card's
    # budget bar shows that running total and never prices summed counts.
    from server.agents.config import AgentConfig
    from server.agents.runtime_support import budget_readout

    readout = budget_readout(
        AgentConfig(),
        None,
        next_turn=1,
        elapsed=0.0,
        usage={"input_tokens": 1_000_000, "cache_read_tokens": 900_000, "cost_usd": 0.62345},
        cost_capped=False,
    )
    assert readout["cost_usd"] == 0.6234


# ── Long-context tier (the GPT cards' 272K break) ──


def test_a_call_over_the_break_bills_all_its_tokens_at_the_long_context_rates():
    rates = get_model_pricing("gpt5.6-sol")
    tier = rates["long_context"]
    above = tier["above"]
    long_call = tracker.call_cost("gpt5.6-sol", above + 1, 1_000, above - 10_000)
    assert long_call == pytest.approx(
        (10_001 * tier["input"] + 1_000 * tier["output"] + (above - 10_000) * tier["cache_read"])
        / 1e6
    )
    at_break = tracker.call_cost("gpt5.6-sol", above, 1_000, above - 10_000)
    assert at_break == pytest.approx(
        estimate_cost("gpt5.6-sol", above, 1_000, above - 10_000, long_context=False)
    )
    assert tracker.is_long_context("gpt5.6-sol", above + 1)
    assert not tracker.is_long_context("gpt5.6-sol", above)


def test_a_running_total_prices_each_call_at_its_own_tier():
    # Two 200K rounds are both short; their summed counts are over the
    # break, and pricing the sum would bill both at the long rate.
    one = tracker.call_cost("gpt6-astra", 200_000, 500, 190_000)
    summed = tracker.call_cost("gpt6-astra", 400_000, 1_000, 380_000)
    assert 2 * one == pytest.approx(estimate_cost("gpt6-astra", 400_000, 1_000, 380_000))
    assert summed > 2 * one


def test_every_gpt_card_has_its_long_context_tier():
    # 2x input and cache, 1.5x output, over 272K input tokens (the cards).
    from server.openai_bedrock.runtime import is_openai_model

    gpt_keys = [k for k in _get_chat_models() if is_openai_model(k)]
    assert gpt_keys
    for key in gpt_keys:
        rates = get_model_pricing(key)
        tier = rates["long_context"]
        assert tier["above"] == 272_000, key
        for field in ("input", "cache_read", "cache_write"):
            assert tier[field] == pytest.approx(2 * rates[field]), (key, field)
        assert tier["output"] == pytest.approx(1.5 * rates["output"]), key


def test_a_claude_model_has_no_long_context_tier():
    assert not tracker.is_long_context("opus5.0", 900_000, 50_000, 10_000)
    assert tracker.call_cost("opus5.0", 900_000, 10) == pytest.approx(
        estimate_cost("opus5.0", 900_000, 10)
    )


def test_a_malformed_long_context_tier_rejects_the_entry():
    base = {"input": 1, "output": 2}
    assert (
        tracker._coerce_pricing_entry({**base, "long_context": {"input": 2, "output": 3}}) is None
    )
    assert (
        tracker._coerce_pricing_entry(
            {**base, "long_context": {"above": "x", "input": 2, "output": 3}}
        )
        is None
    )
    assert tracker._coerce_pricing_entry(
        {**base, "long_context": {"above": 10, "input": 2, "output": 3}}
    )["long_context"] == {
        "above": 10,
        "input": 2.0,
        "output": 3.0,
        "cache_read": 0.0,
        "cache_write": 0.0,
    }


def test_anthropic_cache_buckets_are_additive():
    # Anthropic (cached_in_input=False): input, cache_read and cache_creation
    # are disjoint, so each adds on top — caching does not shrink input.
    base = estimate_cost("sonnet", 1_000_000, 0)
    with_read = estimate_cost("sonnet", 1_000_000, 0, cache_read_tokens=500_000)
    assert with_read == pytest.approx(base + 500_000 / 1e6 * 0.30)


def test_claude_cache_write_is_the_rate_of_the_ttl_the_app_sends():
    # Chat and cron checkpoints carry cache_ttl_for(model id), and Bedrock bills
    # 1-hour writes at 2x input and 5-minute writes at 1.25x. A 1-hour row priced
    # at the 5-minute rate under-counts every write by 37.5%.
    from server.chat.caching import cache_ttl_for
    from server.openai_bedrock.runtime import is_openai_model

    write_multiplier = {"1h": 2.0, "5m": 1.25}
    checked = []
    for key, model_id in _get_chat_models().items():
        if "anthropic.claude" not in str(model_id):
            continue
        rates = get_model_pricing(key)
        ttl = cache_ttl_for(model_id)
        assert rates["cache_write"] == pytest.approx(write_multiplier[ttl] * rates["input"]), (
            f"{key} gets a {ttl} TTL but cache_write is {rates['cache_write']}"
        )
        checked.append(key)
    assert checked


# The cached read as a share of input, where a model's card prices it other
# than 10%: GPT-6.1 Sol's card prices it at $0.11 on $2.20 input (2026-10-03).
_CACHE_READ_SHARE = {"gpt6.1-sol": 0.05}


def test_gpt_cache_rates_follow_the_bedrock_cards():
    # Every Bedrock OpenAI model card prices a cached read at 10% of input
    # unless _CACHE_READ_SHARE says otherwise, and GPT-5.6 / GPT-6 bill their
    # 30-minute cache write at 1.25x input.
    from server.openai_bedrock.runtime import is_openai_model

    gpt_keys = [k for k in _get_chat_models() if is_openai_model(k)]
    assert gpt_keys
    for key in gpt_keys:
        rates = get_model_pricing(key)
        assert rates["cached_in_input"] is True, key
        share = _CACHE_READ_SHARE.get(key, 0.1)
        assert rates["cache_read"] == pytest.approx(share * rates["input"]), key
        if key.startswith(("gpt5.6", "gpt6")):
            assert rates["cache_write"] == pytest.approx(1.25 * rates["input"]), key


# ── File sourcing: pricing.example.json defaults + pricing.json overlay ──


def test_example_file_backs_the_live_table():
    # The committed defaults are what the imported table was loaded from, so a
    # representative rate must come straight from pricing.example.json.
    with open(tracker.PRICING_EXAMPLE_PATH) as f:
        raw = json.load(f)["opus5.0"]
    assert get_model_pricing("opus5.0") == tracker._coerce_pricing_entry(raw)


def test_coerce_entry_normalizes_and_validates():
    # Missing rate fields default to 0.0; cached_in_input kept only when truthy.
    assert tracker._coerce_pricing_entry({"input": 2, "output": 4}) == {
        "input": 2.0,
        "output": 4.0,
        "cache_read": 0.0,
        "cache_write": 0.0,
    }
    assert (
        tracker._coerce_pricing_entry({"input": 1, "output": 2, "cached_in_input": True}).get(
            "cached_in_input"
        )
        is True
    )
    # Missing required field or non-numeric → rejected (None), not a silent 0.
    assert tracker._coerce_pricing_entry({"input": 5}) is None
    assert tracker._coerce_pricing_entry({"input": "x", "output": 1}) is None
    assert tracker._coerce_pricing_entry("nope") is None


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return str(path)


def test_pricing_json_overlays_example(tmp_path, monkeypatch):
    example = _write(
        tmp_path / "pricing.example.json",
        {
            "_notes": {"ignored": "annotation"},
            "opus4.8": {"input": 5, "output": 25},
            "haiku": {"input": 1, "output": 5},
        },
    )
    override = _write(
        tmp_path / "pricing.json",
        {
            "opus4.8": {"input": 4, "output": 20},  # override existing
            "brand-new": {"input": 9, "output": 90},  # add new
        },
    )
    monkeypatch.setattr(tracker, "PRICING_EXAMPLE_PATH", example)
    monkeypatch.setattr(tracker, "PRICING_PATH", override)

    table = tracker._load_pricing()
    assert table["opus4.8"]["input"] == 4.0  # override wins
    assert table["brand-new"]["output"] == 90.0  # new key added
    assert table["haiku"]["input"] == 1.0  # untouched default retained
    assert "_notes" not in table  # annotation key skipped


def test_missing_override_file_falls_back_to_example(tmp_path, monkeypatch):
    example = _write(tmp_path / "pricing.example.json", {"haiku": {"input": 1, "output": 5}})
    monkeypatch.setattr(tracker, "PRICING_EXAMPLE_PATH", example)
    monkeypatch.setattr(tracker, "PRICING_PATH", str(tmp_path / "does-not-exist.json"))
    table = tracker._load_pricing()
    assert set(table) == {"haiku"}


def test_malformed_entry_skipped_others_survive(tmp_path, monkeypatch):
    example = _write(
        tmp_path / "pricing.example.json",
        {
            "good": {"input": 1, "output": 2},
            "bad": {"input": 1},  # missing output → skipped
        },
    )
    monkeypatch.setattr(tracker, "PRICING_EXAMPLE_PATH", example)
    monkeypatch.setattr(tracker, "PRICING_PATH", str(tmp_path / "none.json"))
    table = tracker._load_pricing()
    assert "good" in table and "bad" not in table


def test_unreadable_file_degrades_to_empty(tmp_path, monkeypatch):
    broken = tmp_path / "pricing.example.json"
    broken.write_text("{ not valid json ")
    monkeypatch.setattr(tracker, "PRICING_EXAMPLE_PATH", str(broken))
    monkeypatch.setattr(tracker, "PRICING_PATH", str(tmp_path / "none.json"))
    assert tracker._load_pricing() == {}  # no crash, empty table
