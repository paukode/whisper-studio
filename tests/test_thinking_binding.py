"""Models that bind thinking blocks to their conversation drop a stale block.

Fable 5.2 and Mythos 5.2 refuse a replayed thinking block once the system
prompt or an earlier message changed, and in-turn compaction changes earlier
messages. Each request to such a model that sends a thinking setting must
carry the drop setting and its beta value together; a request to any other
model must carry neither.
"""

from __future__ import annotations

import json
import os

from server.infrastructure.thinking_binding import (
    BINDING_BETA,
    drop_mismatched_thinking,
    enforces_thinking_binding,
)

_EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "config.example.json")


def _claude_catalog() -> dict:
    with open(_EXAMPLE) as f:
        models = json.load(f)["chat_models"]
    return {
        key: entry
        for key, entry in models.items()
        if ".anthropic." in entry["id"] and not entry.get("is_local")
    }


def _drops(body: dict) -> bool:
    binding = (body.get("thinking") or {}).get("block_binding") or {}
    return binding.get("prefix_mismatch_behavior") == "drop_block"


def test_the_setting_and_its_beta_travel_together():
    body = {"thinking": {"type": "adaptive"}}
    drop_mismatched_thinking(body, "global.anthropic.claude-fable-5-2")
    assert _drops(body)
    assert body["anthropic_beta"] == [BINDING_BETA]
    # Applying it again adds nothing.
    drop_mismatched_thinking(body, "global.anthropic.claude-fable-5-2")
    assert body["anthropic_beta"] == [BINDING_BETA]


def test_a_request_without_thinking_is_left_alone():
    body = {"messages": []}
    drop_mismatched_thinking(body, "global.anthropic.claude-mythos-5-2")
    assert body == {"messages": []}


def test_other_models_are_not_opted_into_the_check():
    for model_id in (
        "global.anthropic.claude-fable-5-1",
        "global.anthropic.claude-opus-5-5",
        "global.anthropic.claude-sonnet-5-5",
        "openai.gpt-6.1-sol",
        "",
    ):
        body = {"thinking": {"type": "adaptive"}}
        drop_mismatched_thinking(body, model_id)
        assert body == {"thinking": {"type": "adaptive"}}, model_id


def test_the_chat_engines_body_follows_the_model(monkeypatch):
    from server.chat.engine import anthropic as A

    monkeypatch.setattr(A, "_get_bedrock_client", lambda: object())
    enforcing = 0
    for key, entry in _claude_catalog().items():
        for effort in (None, "high"):
            adapter = A.AnthropicAdapter(
                model_key=key,
                model_id=entry["id"],
                system_prompt="s",
                system_static="s",
                system_dynamic="d",
                caching_on=False,
                cache_ttl="1h",
                effort_label=effort,
                meta=entry,
            )
            body = adapter._build_body([{"role": "user", "content": "hi"}], [], None, 0, False)
            expected = enforces_thinking_binding(entry["id"]) and "thinking" in body
            assert _drops(body) is expected, (key, effort)
            assert (BINDING_BETA in body.get("anthropic_beta", [])) is expected, (key, effort)
            enforcing += expected
    # The shipped catalog has such a model, so the branch above is exercised.
    assert enforcing
