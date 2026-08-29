"""Per-model output ceiling for the Anthropic adapter.

Bedrock rejects requests whose max_tokens exceed the model's output ceiling
(Haiku 4.5: "max_tokens: 128000 > 64000" invalid-request errors killed whole
turns). The adapter must clamp: Haiku-family ids cap at 64K, everything else
keeps the 128K default, and a configured chat_model_meta.max_output wins only
below the ceiling.
"""

import pytest

import server.chat.engine.anthropic as engine_anthropic


def _adapter(monkeypatch, model_id: str, meta: dict):
    monkeypatch.setattr(engine_anthropic, "_get_bedrock_client", lambda: object())
    return engine_anthropic.AnthropicAdapter(
        model_key="k",
        model_id=model_id,
        system_prompt="s",
        system_static="s",
        system_dynamic="",
        caching_on=False,
        cache_ttl="5m",
        effort_label=None,
        force_skill=None,
        loop=None,
        executor=None,
        meta=meta,
    )


@pytest.mark.parametrize(
    ("model_id", "meta", "expected"),
    [
        ("anthropic.claude-haiku-4-5-20251001-v1:0", {}, 64_000),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", {"max_output": 128000}, 64_000),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", {"max_output": 32000}, 32_000),
        ("anthropic.claude-opus-5-v1:0", {}, 128_000),
        ("anthropic.claude-fable-5-v1:0", {"max_output": 128000}, 128_000),
        ("anthropic.claude-sonnet-5-v1:0", {"max_output": "bogus"}, 128_000),
    ],
)
def test_output_ceiling(monkeypatch, model_id, meta, expected):
    assert _adapter(monkeypatch, model_id, meta).max_tokens == expected
