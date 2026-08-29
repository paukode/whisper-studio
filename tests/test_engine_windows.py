"""Context-window resolution and prompt-cap parsing (server/chat/engine).

The turn engine budgets against the model's real input cap: config-supplied
context_window first, family defaults second. parse_prompt_cap classifies a
provider's prompt-too-long rejection by its own reported cap.
"""

from server.chat.engine.windows import (
    FAMILY_DEFAULTS,
    context_window,
    parse_prompt_cap,
)
from server.infrastructure.config import _normalize_chat_models


def test_config_value_wins():
    meta = {"context_window": 123_456, "provider": "openai_bedrock"}
    assert context_window("any", meta) == 123_456


def test_family_default_openai():
    assert context_window("gpt", {"provider": "openai_bedrock"}) == 278_528


def test_family_default_anthropic_and_fallback():
    assert context_window("claude", {"provider": "anthropic"}) == 200_000
    assert context_window("mystery", {}) == 200_000


def test_local_defers_to_runtime():
    assert context_window("local_gemma", {"is_local": True}) is None


def test_parse_prompt_cap_from_mantle_error():
    msg = (
        "prompt tokens (281096) exceed customer model maximum (278528) "
        "for 7903dfda-4910-4bc7-86ca-ea247ecf6bd2"
    )
    assert parse_prompt_cap(msg) == 278_528


def test_parse_prompt_cap_rejects_garbage():
    assert parse_prompt_cap("") is None
    assert parse_prompt_cap("something exploded") is None
    # Tiny numbers are misparses, not real windows.
    assert parse_prompt_cap("exceed model maximum (42)") is None


def test_normalizer_carries_window_metadata():
    ids, meta = _normalize_chat_models(
        {
            "rich": {"id": "openai.gpt-x", "context_window": 278528, "max_output": 64000},
            "plain": {"id": "global.anthropic.claude-y"},
            "legacy": "global.anthropic.claude-z",
        }
    )
    assert meta["rich"]["context_window"] == 278528
    assert meta["rich"]["max_output"] == 64000
    assert meta["plain"]["context_window"] is None  # family default applies later
    assert meta["legacy"]["context_window"] is None
    # Malformed values are dropped, not crashed on.
    _, meta2 = _normalize_chat_models({"bad": {"id": "x", "context_window": "lots"}})
    assert meta2["bad"]["context_window"] is None


def test_example_config_carries_windows():
    import json
    import os

    path = os.path.join(os.path.dirname(__file__), "..", "config.example.json")
    with open(path) as f:
        models = json.load(f)["chat_models"]
    from server.chat.engine.windows import ANTHROPIC_1M_MARKERS

    for key, val in models.items():
        if isinstance(val, dict) and not val.get("is_local"):
            assert val.get("context_window"), f"{key} missing context_window"
            provider = val.get("provider") or (
                "openai_bedrock" if val["id"].startswith("openai.") else "anthropic"
            )
            mid = val["id"].lower()
            expected = (
                1_000_000
                if provider == "anthropic" and any(m in mid for m in ANTHROPIC_1M_MARKERS)
                else FAMILY_DEFAULTS[provider]
            )
            assert val["context_window"] == expected, key


def test_normalizer_lifts_stale_200k_on_1m_family():
    """A config copy that still carries the old template default (200000) on a
    1M-family Claude id reads as 1M — existing user configs upgrade without a
    hand edit. Deliberate other values, and 200K on Haiku, stand."""
    _, meta = _normalize_chat_models(
        {
            "opus5.0": {"id": "global.anthropic.claude-opus-5", "context_window": 200000},
            "haiku": {
                "id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                "context_window": 200000,
            },
            "custom": {"id": "global.anthropic.claude-opus-5", "context_window": 300000},
        }
    )
    assert meta["opus5.0"]["context_window"] == 1_000_000
    assert meta["haiku"]["context_window"] == 200_000
    assert meta["custom"]["context_window"] == 300_000


# ── 1M family, output reserve, per-model compaction thresholds ───────────────


def _cfg(monkeypatch, chat_models=None, meta=None):
    from server.infrastructure import config as config_mod

    cfg = {"chat_models": chat_models or {}, "chat_model_meta": meta or {}}
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)


def test_anthropic_1m_family_by_wire_id(monkeypatch):
    _cfg(
        monkeypatch,
        chat_models={
            "opus5.0": "global.anthropic.claude-opus-5",
            "fable5.0": "global.anthropic.claude-fable-5",
            "sonnet5": "global.anthropic.claude-sonnet-5",
            "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        },
    )
    assert context_window("opus5.0") == 1_000_000
    assert context_window("fable5.0") == 1_000_000
    assert context_window("sonnet5") == 1_000_000
    # Haiku is NOT in the 1M family — verified 64K-output, 200K-window model.
    assert context_window("haiku") == 200_000


def test_output_reserve_mirrors_adapter_clamp(monkeypatch):
    from server.chat.engine.windows import output_reserve

    _cfg(
        monkeypatch,
        chat_models={
            "opus5.0": "global.anthropic.claude-opus-5",
            "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "gpt5.6-sol": "openai.gpt-5.6-sol",
        },
        meta={"gpt5.6-sol": {"provider": "openai_bedrock"}},
    )
    assert output_reserve("opus5.0") == 128_000
    assert output_reserve("haiku") == 64_000
    # GPT-mantle enforces a separate PROMPT cap; nothing is reserved.
    assert output_reserve("gpt5.6-sol") == 0


def test_compact_thresholds_95_and_98_percent(monkeypatch):
    from server.chat.engine.windows import compact_thresholds_chars

    _cfg(
        monkeypatch,
        chat_models={
            "opus5.0": "global.anthropic.claude-opus-5",
            "haiku": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        },
    )
    # Opus 5: (1M - 128K reserve) * 4 chars * 0.95 / 0.98
    trigger, hard = compact_thresholds_chars("opus5.0")
    assert trigger == int(872_000 * 4 * 0.95)
    assert hard == int(872_000 * 4 * 0.98)
    assert trigger < hard
    # Haiku: (200K - 64K) * 4 * fractions — far below the old global 650K.
    trigger, hard = compact_thresholds_chars("haiku")
    assert trigger == int(136_000 * 4 * 0.95)
    assert hard == int(136_000 * 4 * 0.98)


def test_compact_thresholds_none_for_unsized_local(monkeypatch):
    from server.chat.engine.windows import compact_thresholds_chars

    _cfg(monkeypatch, meta={"local_x": {"is_local": True}})
    monkeypatch.setattr("server.local.serving.resident_n_ctx", lambda: None)
    monkeypatch.setattr("server.local.runtime.requested_n_ctx", lambda: None)
    assert compact_thresholds_chars("local_x") is None
