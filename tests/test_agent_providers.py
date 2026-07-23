"""Provider adapters: selection, canonical conversion, usage, structured forcing."""

import asyncio
import json

import pytest

from server.agents.providers.base import TurnUsage, get_adapter, model_key_for_id
from server.agents.providers.openai import canonical_to_responses_items


def test_adapter_selection():
    from server.agents.providers.anthropic import AnthropicBedrockAdapter
    from server.agents.providers.openai import OpenAIBedrockAdapter

    assert isinstance(get_adapter("", "global.anthropic.claude-opus-4-8"), AnthropicBedrockAdapter)
    # id-substring fallback: no key needed to route an openai id correctly
    assert isinstance(get_adapter("", "bedrock-mantle.openai.gpt-5.6-sol"), OpenAIBedrockAdapter)


def test_model_key_reverse_lookup(monkeypatch):
    from server.infrastructure import config as config_mod

    real = config_mod.load_config

    def _patched():
        cfg = dict(real())
        cfg["chat_models"] = {"opus4.8": "global.anthropic.claude-opus-4-8"}
        return cfg

    monkeypatch.setattr(config_mod, "load_config", _patched)
    assert model_key_for_id("global.anthropic.claude-opus-4-8") == "opus4.8"
    assert model_key_for_id("unknown-id") == ""


def test_turn_usage_accumulation():
    total = TurnUsage()
    total.add(TurnUsage(input_tokens=100, output_tokens=10, cache_read_tokens=50))
    total.add(TurnUsage(input_tokens=20, output_tokens=5, cache_creation_tokens=7))
    assert total.as_dict() == {
        "input_tokens": 120,
        "output_tokens": 15,
        "cache_read_tokens": 50,
        "cache_creation_tokens": 7,
    }


def test_canonical_to_responses_items_roundtrip_shapes():
    messages = [
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "s"},
                {"type": "text", "text": "on it"},
                {"type": "tool_use", "id": "call_1", "name": "ws_grep", "input": {"q": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "match found"}],
        },
    ]
    items = canonical_to_responses_items(messages)
    kinds = [(i.get("type") or i.get("role")) for i in items]
    assert kinds[0] == "user"
    assert "function_call" in kinds
    fc = next(i for i in items if i.get("type") == "function_call")
    assert fc["call_id"] == "call_1"
    assert json.loads(fc["arguments"]) == {"q": "x"}
    # thinking blocks never cross the provider boundary
    assert not any("thinking" in json.dumps(i) for i in items)
    # assistant text precedes its function_call
    assistant_idx = next(
        n for n, i in enumerate(items) if i.get("role") == "assistant" and "on it" in str(i)
    )
    fc_idx = items.index(fc)
    assert assistant_idx < fc_idx


class _FakeBody:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload


class _FakeBedrock:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        self.requests.append(json.loads(kwargs["body"]))
        return {"body": _FakeBody(self._responses.pop(0))}


@pytest.fixture
def anthropic_adapter(monkeypatch):
    from server.agents.providers.anthropic import AnthropicBedrockAdapter

    adapter = AnthropicBedrockAdapter(model_key="opus4.8", model_id="test-model")
    fake = _FakeBedrock(
        [
            {
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "let me think", "signature": "sig"},
                    {"type": "redacted_thinking", "data": "opaque"},
                    {"type": "text", "text": "answer"},
                ],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                },
            }
        ]
    )
    adapter._bedrock = fake
    return adapter, fake


def test_anthropic_adapter_effort_usage_and_redacted_thinking(anthropic_adapter):
    adapter, fake = anthropic_adapter
    turn = asyncio.run(
        adapter.invoke(
            system="sys",
            messages=[{"role": "user", "content": "q"}],
            tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
            max_tokens=512,
            effort_label="high",
        )
    )
    body = fake.requests[0]
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert turn.usage.as_dict() == {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_tokens": 3,
        "cache_creation_tokens": 2,
    }
    # redacted_thinking preserved for replay
    types = [b["type"] for b in turn.assistant_blocks]
    assert "redacted_thinking" in types
    assert turn.text == "answer"


def test_anthropic_adapter_structured_forcing_omits_thinking(anthropic_adapter):
    adapter, fake = anthropic_adapter
    fake._responses = [
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "emit_result",
                    "input": {"verdict": "ok"},
                }
            ],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }
    ]
    schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    turn = asyncio.run(
        adapter.invoke(
            system="sys",
            messages=[{"role": "user", "content": "q"}],
            tools=None,
            max_tokens=256,
            effort_label="high",  # must be dropped: forced tool_choice + thinking is rejected
            force_structured=schema,
        )
    )
    body = fake.requests[-1]
    assert "thinking" not in body
    assert body["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert body["tools"][0]["input_schema"] == schema
    assert turn.structured_output == {"verdict": "ok"}
    assert turn.tool_calls == []  # emit_result is consumed, not dispatched


def test_openai_tier_offers_ultracode():
    from server.infrastructure.effort import EFFORT_TIERS, clamp_effort, is_ultracode

    assert "ultracode" in EFFORT_TIERS["openai"]
    assert clamp_effort("ultracode", EFFORT_TIERS["openai"]) == "ultracode"
    assert is_ultracode(clamp_effort("ultracode", EFFORT_TIERS["openai"]))
    # standard tier still clamps it away
    assert clamp_effort("ultracode", EFFORT_TIERS["standard"]) == "max"


def test_run_agent_structured_schema_end_to_end(monkeypatch):
    """The loop finishes naturally, then one forced call distills a validated
    structured object into AgentResult.structured_output."""
    from server.agents.runtime import run_agent

    fake = _FakeBedrock(
        [
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "the answer is 4"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "s1",
                        "name": "emit_result",
                        "input": {"answer": 4},
                    }
                ],
                "usage": {"input_tokens": 8, "output_tokens": 3},
            },
        ]
    )
    monkeypatch.setattr("server.chat._get_bedrock_client", lambda: fake)
    monkeypatch.setattr("server.chat.assemble_tool_pool", lambda *a, **k: [])
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    result = asyncio.run(
        run_agent(
            "what is 2+2",
            agent_type="general",
            session_id="",
            model_id_override="test-model",
            structured_schema=schema,
        )
    )
    assert result.status == "completed"
    assert result.structured_output == {"answer": 4}
    # usage aggregated across BOTH calls (loop turn + distillation)
    assert result.usage["input_tokens"] == 18
    assert result.usage["output_tokens"] == 8
    # the distillation request forced emit_result without thinking
    body = fake.requests[-1]
    assert body["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert "thinking" not in body
