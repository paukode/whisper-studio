"""Cloud (Bedrock) prompt-caching: prompt split, cache-control shaping, pricing.

These exercise the deterministic pieces (no Bedrock needed). The live
`cache_read_input_tokens > 0` check belongs to manual verification against a real
endpoint (see PROMPT_CACHING_PLAN.md), not here.
"""

import itertools

import pytest

from server.chat.caching import cache_ttl_for, cached_tools_and_system
from server.costs.tracker import estimate_cost
from server.infrastructure.feature_flags import get_flag_defaults
from server.prompts import PromptLayer, build_system_prompt, build_system_prompt_split, get_registry

# ── The correctness gate: split is byte-identical to the legacy string ────────

_KW_GRID = list(
    itertools.product(
        [False, True],  # brief_mode
        [False, True],  # plan_mode
        [None, "/tmp/ws"],  # ws_path
        [False, True],  # ultracode
        ["", "recalled memory"],  # memory_context
        ["", "PROJECT INSTRUCTIONS"],  # whisper_md_context
        ["", "session summary"],  # session_memory_context
    )
)


@pytest.mark.parametrize("brief,plan,ws,ultra,mem,whisper,sess", _KW_GRID)
def test_split_is_byte_identical_to_string(brief, plan, ws, ultra, mem, whisper, sess):
    kw = dict(
        ws_path=ws,
        session_id="s",
        brief_mode=brief,
        plan_mode=plan,
        whisper_md_context=whisper,
        memory_context=mem,
        session_memory_context=sess,
        ultracode=ultra,
    )
    static, dynamic = build_system_prompt_split(**kw)
    assert static + dynamic == build_system_prompt(**kw)


def test_static_holds_identity_and_dynamic_holds_memory():
    static, dynamic = build_system_prompt_split(
        ws_path=None,
        session_id="s",
        memory_context="REMEMBER THIS FACT",
    )
    # Memory is a DYNAMIC-layer section, so it must land in the uncached tail.
    assert "REMEMBER THIS FACT" in dynamic
    assert "REMEMBER THIS FACT" not in static
    # The static block is non-trivial (identity etc.) so it can clear the
    # cumulative cache minimum once tools precede it.
    assert len(static) > 1000


def test_no_dynamic_layer_section_uses_prepend():
    """Byte-equality of the split relies on no dynamic-layer (>=40) section
    using prepend (the legacy builder front-loads all prepend sections)."""
    for section in get_registry().get_sections():
        if int(section.layer) >= int(PromptLayer.DYNAMIC):
            assert not section.prepend, f"dynamic section {section.name!r} uses prepend"


# ── TTL per model ─────────────────────────────────────────────────────────────


def test_cache_ttl_per_model():
    assert cache_ttl_for("global.anthropic.claude-opus-4-6-v1") == "5m"
    assert cache_ttl_for("global.anthropic.claude-sonnet-4-6") == "5m"
    assert cache_ttl_for("global.anthropic.claude-opus-4-7") == "1h"
    assert cache_ttl_for("global.anthropic.claude-opus-4-8") == "1h"
    assert cache_ttl_for("global.anthropic.claude-haiku-4-5-20251001-v1:0") == "1h"
    assert cache_ttl_for("global.anthropic.claude-fable-5") == "1h"
    assert cache_ttl_for("") == "1h"  # unknown -> default to the cheaper-to-keep 1h


# ── cache_control shaping ─────────────────────────────────────────────────────


def test_cached_tools_and_system_shapes():
    tools = [
        {"name": "a", "description": "", "input_schema": {}},
        {"name": "b", "description": "", "input_schema": {}},
    ]
    tools_field, system_field = cached_tools_and_system(tools, "STATIC", "DYNAMIC", "1h")

    # cache_control only on the LAST tool; the shared pool is not mutated.
    assert "cache_control" not in tools_field[0]
    assert tools_field[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in tools[0] and "cache_control" not in tools[1]

    # system: static cached, dynamic uncached.
    assert len(system_field) == 2
    assert system_field[0]["text"] == "STATIC"
    assert system_field[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert system_field[1] == {"type": "text", "text": "DYNAMIC"}
    assert "cache_control" not in system_field[1]


def test_cached_system_omits_empty_dynamic_block():
    tools = [{"name": "a", "description": "", "input_schema": {}}]
    _, system_field = cached_tools_and_system(tools, "STATIC", "", "5m")
    assert len(system_field) == 1  # no empty content block
    assert system_field[0]["text"] == "STATIC"


# ── cache-aware cost ──────────────────────────────────────────────────────────


def test_estimate_cost_cache_terms():
    # Opus 4.6: input $5/1M, output $25/1M (version-keyed, no generic "opus")
    base = estimate_cost("opus4.6", 1000, 500)
    assert base == pytest.approx(1000 / 1e6 * 5 + 500 / 1e6 * 25)

    # cache read billed at the model's cache_read rate ($0.50/1M)
    read = estimate_cost("opus4.6", 1000, 500, cache_read_tokens=8000)
    assert read - base == pytest.approx(8000 / 1e6 * 0.50)

    # cache write billed at the model's cache_write rate ($6.25/1M)
    write = estimate_cost("opus4.6", 1000, 500, cache_creation_tokens=8000)
    assert write - base == pytest.approx(8000 / 1e6 * 6.25)

    # zero cache tokens => identical to the legacy 3-arg result
    assert estimate_cost("opus4.6", 1000, 500, 0, 0) == base


# ── feature flag ──────────────────────────────────────────────────────────────


def test_prompt_caching_flag_registered_on_by_default():
    # Enabled by default (commit "caching: enable Bedrock prompt caching by
    # default"). Bedrock caches tool defs + static system to cut multi-turn
    # input-token cost; users can still toggle it off in the feature-flags tab.
    assert get_flag_defaults().get("prompt_caching") is True
