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
    for key, val in models.items():
        if isinstance(val, dict) and not val.get("is_local"):
            assert val.get("context_window"), f"{key} missing context_window"
            provider = val.get("provider") or (
                "openai_bedrock" if val["id"].startswith("openai.") else "anthropic"
            )
            assert val["context_window"] == FAMILY_DEFAULTS[provider]
