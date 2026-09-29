"""No request the app builds forces a tool call.

Opus 5.5, Sonnet 5.5, Fable 5.1 and Mythos 5.1 answer a forced tool_choice
(``{"type": "tool"}`` or ``{"type": "any"}``) with a 400, so the model always
decides which tool to call: a requested skill and the structured-output
emit_result tool are asked for in the prompt instead. "auto" and a final
round's "none" (tools off) are the only choices any builder may send.

Each cloud builder is driven across every shipped model of its provider and
the turn shapes it distinguishes (first or later round, final round, caching,
effort, a structured call), so a model or a branch added later is covered
without editing a list here; the on-device one across both of its tool_choice
modes.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import os
import types

import pytest

_EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "config.example.json")
_TOOLS = [
    {"name": "summarize_notes", "description": "d", "input_schema": {"type": "object"}},
    {"name": "web_search", "description": "d", "input_schema": {"type": "object"}},
]
_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
_MESSAGES = [{"role": "user", "content": "Call the `summarize_notes` tool now."}]


def _forces(choice) -> bool:
    """True for any tool_choice that makes the model call a tool, in the
    Anthropic, OpenAI and Converse spellings alike."""
    if choice is None:
        return False
    if isinstance(choice, str):
        return choice not in ("auto", "none")
    if isinstance(choice, dict):
        if "type" in choice:
            return choice["type"] not in ("auto", "none")
        return not set(choice) <= {"auto", "none"}
    return True


def _catalog(provider: str) -> dict:
    with open(_EXAMPLE) as f:
        models = json.load(f)["chat_models"]
    out = {}
    for key, entry in models.items():
        is_openai = entry["id"].startswith("openai.") or entry.get("provider") == "openai_bedrock"
        if entry.get("is_local") or is_openai != (provider == "openai"):
            continue
        out[key] = entry
    assert out, provider
    return out


def test_the_predicate_recognizes_every_forcing_spelling():
    for forced in (
        {"type": "tool", "name": "x"},
        {"type": "any"},
        {"type": "function", "name": "x"},
        "required",
        {"tool": {"name": "x"}},
        {"any": {}},
    ):
        assert _forces(forced), forced
    for free in (None, "auto", "none", {"type": "auto"}, {"type": "none"}, {"auto": {}}):
        assert not _forces(free), free


def test_the_chat_engines_claude_body_never_forces(monkeypatch):
    from server.chat.engine import anthropic as A

    monkeypatch.setattr(A, "_get_bedrock_client", lambda: object())
    checked = 0
    for key, entry in _catalog("anthropic").items():
        for effort, caching, round_num, last in itertools.product(
            (None, "high"), (False, True), (0, 1), (False, True)
        ):
            adapter = A.AnthropicAdapter(
                model_key=key,
                model_id=entry["id"],
                system_prompt="s" * 10,
                system_static="s" * 10,
                system_dynamic="d",
                caching_on=caching,
                cache_ttl="1h",
                effort_label=effort,
                loop=None,
                executor=None,
                meta=entry,
            )
            body = adapter._build_body(list(_MESSAGES), list(_TOOLS), None, round_num, last)
            assert not _forces(body.get("tool_choice")), (key, effort, round_num, last)
            checked += 1
    assert checked


class _Body(io.BytesIO):
    pass


class _Bedrock:
    """invoke_model that records each body and answers with emit_result."""

    def __init__(self):
        self.bodies: list[dict] = []

    def invoke_model(self, **kwargs):
        self.bodies.append(json.loads(kwargs["body"]))
        payload = {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t1", "name": "emit_result", "input": {}}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        return {"body": _Body(json.dumps(payload).encode())}


def test_the_agent_claude_adapter_never_forces_and_offers_only_emit_result():
    from server.agents.providers.anthropic import STRUCTURED_TOOL_NAME, AnthropicBedrockAdapter

    for key, entry in _catalog("anthropic").items():
        adapter = AnthropicBedrockAdapter(model_key=key, model_id=entry["id"])
        adapter._bedrock = _Bedrock()
        for structured, effort in itertools.product((None, _SCHEMA), (None, "high")):
            asyncio.run(
                adapter.invoke(
                    system="s",
                    messages=list(_MESSAGES),
                    tools=list(_TOOLS),
                    max_tokens=64,
                    effort_label=effort,
                    force_structured=structured,
                )
            )
            body = adapter._bedrock.bodies[-1]
            assert not _forces(body.get("tool_choice")), (key, structured, effort)
            if structured is not None:
                # One tool, which the model decides to call; thinking unset,
                # the one setting every catalog model accepts.
                assert [t["name"] for t in body["tools"]] == [STRUCTURED_TOOL_NAME]
                assert body["tool_choice"] == {"type": "auto"}
                assert "thinking" not in body


class _Stream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e

        return gen()

    async def close(self):
        return None


def _fake_mantle(monkeypatch, text):
    import server.openai_bedrock.runtime as oai

    posted: list[dict] = []
    events = [
        types.SimpleNamespace(type="response.output_text.delta", delta=text),
        types.SimpleNamespace(
            type="response.completed",
            response=types.SimpleNamespace(
                usage=types.SimpleNamespace(input_tokens=1, output_tokens=1)
            ),
        ),
    ]

    async def create(**kwargs):
        posted.append(kwargs)
        return _Stream(list(events))

    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
    monkeypatch.setattr(oai, "build_client", lambda region: client)
    return posted


def test_the_chat_engines_gpt_request_never_forces(monkeypatch):
    from server.chat.engine.openai import OpenAIResponsesAdapter

    posted = _fake_mantle(monkeypatch, "done")

    async def drain(agen):
        return [x async for x in agen]

    for key, entry in _catalog("openai").items():
        for last in (False, True):
            adapter = OpenAIResponsesAdapter(
                model_key=key, model_id=entry["id"], system_prompt="s", effort_label="high"
            )
            asyncio.run(drain(adapter.stream_round(list(_MESSAGES), list(_TOOLS), None, 0, last)))
            assert not _forces(posted[-1].get("tool_choice")), (key, last)
    assert posted


def test_the_agent_gpt_adapter_never_forces(monkeypatch):
    from server.agents.providers.openai import OpenAIBedrockAdapter

    posted = _fake_mantle(monkeypatch, '{"ok": true}')
    for key, entry in _catalog("openai").items():
        adapter = OpenAIBedrockAdapter(model_key=key, model_id=entry["id"])
        for structured in (None, _SCHEMA):
            asyncio.run(
                adapter.invoke(
                    system="s",
                    messages=list(_MESSAGES),
                    tools=list(_TOOLS),
                    max_tokens=64,
                    effort_label="high",
                    force_structured=structured,
                )
            )
            assert not _forces(posted[-1].get("tool_choice")), (key, structured)


@pytest.mark.parametrize("honors_tool_choice", [True, False])
def test_the_on_device_request_never_forces(honors_tool_choice):
    from server.chat.engine.local import LocalAdapter

    adapter = LocalAdapter(
        model_key="k",
        base_url="http://x",
        system_prompt="s",
        thinking=False,
        tools_enabled=True,
        supports_tool_choice=honors_tool_choice,
    )
    for last in (False, True):
        body = adapter.wire_request(list(_MESSAGES), list(_TOOLS), last)
        assert not _forces(body.get("tool_choice")), last
