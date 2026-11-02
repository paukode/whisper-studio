"""Agent provider contract over every cloud chat model in the config.

For each Claude and GPT model the normalized config lists (read at test
time, never a frozen list), the real agent runtime (run_agent) must build a
valid request with tools, cache the Claude prompt prefix, keep the effort on
the model's own ladder, parse tool calls, distill a structured result, and
return a proper report when the deadline stops it. A GPT parent with a
Claude child and the reverse are covered too. Fake transports only: no
request leaves the process.
"""

import asyncio
import copy
import dataclasses
import json
import re
import types

import pytest

from server.agents import journal as journal_mod
from server.agents.config import get_agent_config
from server.agents.runtime import run_agent
from server.tasks import registry, shell
from tests.cost_rows import session_cost_rows
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}
# The reasoning.effort values bedrock-mantle accepts for GPT models.
_GPT_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_CACHE_KEYS = ("cache_control",)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "storage_root", lambda: str(tmp_path / "storage"))
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)
    monkeypatch.setattr(
        "server.costs.budget.check_budget_soft", lambda session_id, fraction=0.9: None
    )
    # The retention gate reads the Bedrock control plane; it has its own tests.
    monkeypatch.setattr(
        "server.infrastructure.data_retention.retention_block_reason", lambda model_id: None
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)


# ── the model list, from the normalized config ─────────────────────────────


def _cloud_models() -> list[tuple[str, str, str, dict]]:
    """(key, id, provider, meta) of every cloud Claude and GPT chat model."""
    from server.infrastructure.config import load_config
    from server.local.runtime import is_local_model_id

    cfg = load_config()
    meta_all = cfg.get("chat_model_meta") or {}
    out = []
    for key, model_id in (cfg.get("chat_models") or {}).items():
        meta = meta_all.get(key) or {}
        if meta.get("is_local") or is_local_model_id(model_id):
            continue
        provider = meta.get("provider", "anthropic")
        if provider in ("anthropic", "openai_bedrock"):
            out.append((key, model_id, provider, meta))
    return out


def _claude_models():
    return [m for m in _cloud_models() if m[2] == "anthropic"]


def _gpt_models():
    return [m for m in _cloud_models() if m[2] == "openai_bedrock"]


def test_the_config_lists_claude_and_gpt_models():
    # The contract below is empty, and proves nothing, without both.
    assert _claude_models() and _gpt_models()


# ── fake transports ─────────────────────────────────────────────────────────


class _Body:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw


class _FakeInvoke:
    """Non-streaming invoke_model (the structured-output call)."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.requests: list[dict] = []

    def invoke_model(self, **kwargs):
        self.requests.append(json.loads(kwargs["body"]))
        return {"body": _Body(self._payloads.pop(0))}


def _emit_payload(result: dict) -> dict:
    return {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "e1", "name": "emit_result", "input": result}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }


class _Ev:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Stream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e

        return gen()

    async def close(self):
        pass


class _FakeResponses:
    """responses.create for the engine rounds and the structured call."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.captured: list[dict] = []

    async def create(self, **kwargs):
        self.captured.append(copy.deepcopy(kwargs))
        if not self.rounds:
            raise RuntimeError("no scripted rounds left")
        return _Stream(self.rounds.pop(0))


def _gpt_usage(inp=40, out=5, cached=30):
    return _Ev(input_tokens=inp, output_tokens=out, input_tokens_details=_Ev(cached_tokens=cached))


def _gpt_tool_round(call_id="c1", item_id="i1"):
    args = json.dumps({"pattern": "x"})
    return [
        _Ev(
            type="response.output_item.added",
            item=_Ev(type="function_call", id=item_id, call_id=call_id, name="ws_grep"),
        ),
        _Ev(type="response.function_call_arguments.delta", item_id=item_id, delta=args),
        _Ev(type="response.function_call_arguments.done", item_id=item_id, arguments=args),
        _Ev(
            type="response.output_item.done",
            item=_Ev(
                type="function_call", id=item_id, call_id=call_id, name="ws_grep", arguments=args
            ),
        ),
        _Ev(type="response.completed", response=_Ev(usage=_gpt_usage())),
    ]


def _gpt_text_round(text):
    return [
        _Ev(type="response.output_text.delta", delta=text),
        _Ev(type="response.completed", response=_Ev(usage=_gpt_usage())),
    ]


def _claude_tool_round(tool_id="t1", cache_read=0, cache_creation=0):
    return [
        msg_start(input_tokens=40, cache_read=cache_read, cache_creation=cache_creation),
        *tool_use_block(tool_id, "ws_grep", {"pattern": "x"}),
        *msg_end("tool_use"),
    ]


def _claude_text_round(text, cache_read=0):
    return [msg_start(input_tokens=40, cache_read=cache_read), *text_block(text), *msg_end()]


def _install(monkeypatch, *, claude_rounds=(), distill=(), gpt_rounds=(), route_tool=None):
    """Fake both providers' transports, at the bindings production reads."""
    import server.openai_bedrock.runtime as oai

    stream = FakeBedrockClient(list(claude_rounds))
    invoke = _FakeInvoke(list(distill))
    responses = _FakeResponses(list(gpt_rounds))
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: stream)
    monkeypatch.setattr("server.chat.infra._get_bedrock_client", lambda: invoke)
    monkeypatch.setattr(
        oai, "build_client", lambda region: types.SimpleNamespace(responses=responses)
    )
    monkeypatch.setattr(oai, "region_for", lambda model_key: "us-east-1")
    calls: list[dict] = []

    async def _route(tool_name, tool_input, **kw):
        calls.append({"name": tool_name, "input": dict(tool_input)})
        if route_tool is not None:
            return await route_tool(tool_name, tool_input, **kw)
        return "1 match in a.py", []

    monkeypatch.setattr("server.tool_executor.route_tool", _route)
    return stream, invoke, responses, calls


# ── request checks ──────────────────────────────────────────────────────────


def _strip_cache(obj):
    """The request with every cache_control marker removed."""
    if isinstance(obj, dict):
        return {k: _strip_cache(v) for k, v in obj.items() if k not in _CACHE_KEYS}
    if isinstance(obj, list):
        return [_strip_cache(v) for v in obj]
    return obj


def _normalized(messages: list) -> list:
    """Messages without cache markers, string content as its one text block.

    The moving checkpoint turns a string last message into a text block list
    to carry the marker; Bedrock reads both forms as the same prompt."""
    out = []
    for msg in _strip_cache(messages):
        content = msg.get("content")
        if isinstance(content, str):
            msg = {**msg, "content": [{"type": "text", "text": content}]}
        out.append(msg)
    return out


def _breakpoints(body: dict) -> int:
    return json.dumps(body).count('"cache_control"')


def _claude_efforts(meta: dict, key: str) -> set[str]:
    from server.infrastructure.effort import api_effort_for, effort_levels_for

    return {api_effort_for(level, meta, key) for level in effort_levels_for(meta, key)}


def _check_claude_effort(body: dict, meta: dict, key: str) -> None:
    from server.infrastructure.effort import effort_levels_for

    if not effort_levels_for(meta, key):
        # No effort support (Haiku): neither thinking nor output_config.
        assert "thinking" not in body and "output_config" not in body, key
        return
    assert body["thinking"]["type"] == "adaptive", key
    assert body["output_config"]["effort"] in _claude_efforts(meta, key), key


def _check_claude_cached_rounds(first: dict, second: dict, key: str) -> None:
    # Round 1: the tools, the system block and the last message are checkpointed.
    assert first["tools"] and "cache_control" in first["tools"][-1], key
    assert isinstance(first["system"], list), key
    assert "cache_control" in first["system"][0], key
    assert "cache_control" in json.dumps(first["messages"][-1]), key
    # Bedrock allows at most four cache breakpoints per request.
    assert _breakpoints(first) <= 4 and _breakpoints(second) <= 4, key
    # Round 2 sends the same prefix: identical tools and system, and round 1's
    # messages unchanged at the head of round 2's (only the moving checkpoint
    # moved to the new last message).
    assert second["tools"] == first["tools"], key
    assert second["system"] == first["system"], key
    head = _normalized(first["messages"])
    assert _normalized(second["messages"][: len(head)]) == head, key
    assert len(second["messages"]) > len(head), key
    assert "cache_control" in json.dumps(second["messages"][-1]), key


# ── Claude: every model ─────────────────────────────────────────────────────


def test_every_claude_model_runs_an_agent_with_a_cached_prefix(monkeypatch):
    for key, model_id, _, meta in _claude_models():
        stream, invoke, _, calls = _install(
            monkeypatch,
            claude_rounds=[
                _claude_tool_round(cache_creation=30),
                _claude_text_round("FINDINGS: 4.", cache_read=30),
            ],
            distill=[_emit_payload({"answer": 4})],
        )
        session = f"contract-{key}"
        result = asyncio.run(
            run_agent(
                "find the answer",
                agent_type="general",
                session_id=session,
                model_id_override=model_id,
                effort_label="ultracode",
                structured_schema=_SCHEMA,
            )
        )
        assert result.status == "completed", (key, result.output)
        assert calls and calls[0]["name"] == "ws_grep", key
        assert calls[0]["input"]["pattern"] == "x", key
        assert result.structured_output == {"answer": 4}, key
        first, second = stream.requests
        assert {t["name"] for t in first["tools"]} >= {"ws_grep"}, key
        for body in (first, second):
            _check_claude_effort(body, meta, key)
        _check_claude_cached_rounds(first, second, key)
        # The structured call: one emit_result tool, offered (never forced),
        # its schema the object schema the caller gave, and its prefix cached.
        distill_body = invoke.requests[-1]
        assert [t["name"] for t in distill_body["tools"]] == ["emit_result"], key
        assert distill_body["tools"][0]["input_schema"] == _SCHEMA, key
        assert distill_body["tool_choice"] == {"type": "auto"}, key
        assert "cache_control" in distill_body["system"][0], key
        assert _breakpoints(distill_body) <= 4, key
        # Cache reads and writes reach the run's usage and the cost log.
        assert result.usage["cache_read_tokens"] == 30, key
        assert result.usage["cache_creation_tokens"] == 30, key
        rows = session_cost_rows(session)
        assert sum(r["cache_read_tokens"] for r in rows) == 30, key


def test_every_claude_model_reports_when_the_deadline_stops_it(monkeypatch):
    async def _slow(tool_name, tool_input, **kw):
        await asyncio.sleep(0.25)
        return "ok", []

    cfg = dataclasses.replace(get_agent_config("general"), max_turns=10, deadline_seconds=0.4)
    for key, model_id, _, _ in _claude_models():
        stream, _, _, _ = _install(
            monkeypatch,
            claude_rounds=[
                _claude_tool_round("t1"),
                _claude_tool_round("t2"),
                _claude_text_round("FINDINGS: partial."),
            ],
            route_tool=_slow,
        )
        result = asyncio.run(
            run_agent("go", config=cfg, session_id=f"dl-{key}", model_id_override=model_id)
        )
        assert result.stop_reason == "deadline", key
        assert result.output.startswith("[Agent stopped - reached time limit]"), key
        assert "FINDINGS: partial." in result.output, key
        # The report round has no tools and no cache checkpoints.
        assert "tools" not in stream.requests[-1], key


# ── GPT: every model ────────────────────────────────────────────────────────


def _check_gpt_request(req: dict, key: str) -> None:
    assert req["reasoning"]["effort"] in _GPT_EFFORTS, key
    assert req["prompt_cache_key"].startswith("ws-"), key
    names = {t["name"] for t in req.get("tools") or []}
    assert "ws_grep" in names, key
    for tool in req["tools"]:
        # Responses function tools are FLAT, with an object parameters schema.
        assert tool["type"] == "function" and "function" not in tool, key
        assert tool["parameters"].get("type") == "object", key


def _check_gpt_structured(req: dict, key: str) -> None:
    fmt = req["text"]["format"]
    assert fmt["type"] == "json_schema", key
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fmt["name"]), key
    assert fmt["schema"] == _SCHEMA and fmt["schema"]["type"] == "object", key
    # strict mode needs additionalProperties false and every property
    # required; the app sends strict false, so any object schema is valid.
    if fmt.get("strict"):
        assert fmt["schema"].get("additionalProperties") is False, key
    assert "tools" not in req, key


def test_every_gpt_model_runs_an_agent_and_keeps_its_cache_key(monkeypatch):
    for key, model_id, _, _ in _gpt_models():
        _, _, responses, calls = _install(
            monkeypatch,
            gpt_rounds=[
                _gpt_tool_round(),
                _gpt_text_round("FINDINGS: 4."),
                _gpt_text_round('{"answer": 4}'),
            ],
        )
        session = f"contract-{key}"
        result = asyncio.run(
            run_agent(
                "find the answer",
                agent_type="general",
                session_id=session,
                model_id_override=model_id,
                effort_label="ultracode",
                structured_schema=_SCHEMA,
            )
        )
        assert result.status == "completed", (key, result.output)
        assert calls and calls[0]["name"] == "ws_grep", key
        assert result.structured_output == {"answer": 4}, key
        first, second, distill = responses.captured
        _check_gpt_request(first, key)
        _check_gpt_request(second, key)
        # One session, one cache key: round 2 is routed to round 1's cache.
        assert second["prompt_cache_key"] == first["prompt_cache_key"], key
        assert second["instructions"] == first["instructions"], key
        assert second["tools"] == first["tools"], key
        assert second["input"][: len(first["input"])] == first["input"], key
        _check_gpt_structured(distill, key)
        # The cached input reaches the run's usage and the cost log.
        assert result.usage["cache_read_tokens"] == 90, key
        rows = session_cost_rows(session)
        assert sum(r["cache_read_tokens"] for r in rows) == 90, key


def test_every_gpt_model_reports_when_the_deadline_stops_it(monkeypatch):
    async def _slow(tool_name, tool_input, **kw):
        await asyncio.sleep(0.25)
        return "ok", []

    cfg = dataclasses.replace(get_agent_config("general"), max_turns=10, deadline_seconds=0.4)
    for key, model_id, _, _ in _gpt_models():
        _, _, responses, _ = _install(
            monkeypatch,
            gpt_rounds=[
                _gpt_tool_round("c1", "i1"),
                _gpt_tool_round("c2", "i2"),
                _gpt_text_round("FINDINGS: partial."),
            ],
            route_tool=_slow,
        )
        result = asyncio.run(
            run_agent("go", config=cfg, session_id=f"dl-{key}", model_id_override=model_id)
        )
        assert result.stop_reason == "deadline", key
        assert result.output.startswith("[Agent stopped - reached time limit]"), key
        assert "FINDINGS: partial." in result.output, key
        last = responses.captured[-1]
        assert last.get("tool_choice") in (None, "none"), key


# ── mixed trees: the child runs on its own provider and ladder ──────────────


def _child_id(child_key: str, parent_id: str) -> str:
    from server.agents.model_resolve import resolve_model_override

    child_id, resolved_key, _ = resolve_model_override(child_key, parent_id)
    assert resolved_key == child_key
    return child_id


@pytest.mark.parametrize("parent_effort", ["ultracode", "max", "extra", "low"])
def test_a_gpt_parent_with_a_claude_child_override(monkeypatch, parent_effort):
    parent_key, parent_id, _, _ = _gpt_models()[0]
    for key, _, _, meta in _claude_models():
        stream, _, responses, calls = _install(
            monkeypatch,
            claude_rounds=[_claude_tool_round(), _claude_text_round("FINDINGS: ok.")],
        )
        result = asyncio.run(
            run_agent(
                "child work",
                agent_type="general",
                session_id=f"mix-{parent_key}-{key}",
                model_id_override=_child_id(key, parent_id),
                effort_label=parent_effort,
            )
        )
        assert result.status == "completed", (key, result.output)
        assert calls[0]["name"] == "ws_grep", key
        # Every call went to Claude; none to the parent's GPT endpoint.
        assert len(stream.requests) == 2 and not responses.captured, key
        for body in stream.requests:
            _check_claude_effort(body, meta, key)
        _check_claude_cached_rounds(stream.requests[0], stream.requests[1], key)


@pytest.mark.parametrize("parent_effort", ["ultracode", "max", "extra", "low"])
def test_a_claude_parent_with_a_gpt_child_override(monkeypatch, parent_effort):
    parent_key, parent_id, _, _ = _claude_models()[0]
    for key, _, _, _ in _gpt_models():
        stream, _, responses, calls = _install(
            monkeypatch,
            gpt_rounds=[_gpt_tool_round(), _gpt_text_round("FINDINGS: ok.")],
        )
        result = asyncio.run(
            run_agent(
                "child work",
                agent_type="general",
                session_id=f"mix-{parent_key}-{key}",
                model_id_override=_child_id(key, parent_id),
                effort_label=parent_effort,
            )
        )
        assert result.status == "completed", (key, result.output)
        assert calls[0]["name"] == "ws_grep", key
        assert len(responses.captured) == 2 and not stream.requests, key
        for req in responses.captured:
            _check_gpt_request(req, key)


# ── the provider adapters on their own (structured call, tool parsing) ──────


def test_the_claude_structured_retry_reuses_the_cached_prefix(monkeypatch):
    from server.agents.providers.anthropic import AnthropicBedrockAdapter

    tools = [{"name": "ws_grep", "description": "grep", "input_schema": {"type": "object"}}]
    for key, model_id, _, _ in _claude_models():
        fake = _FakeInvoke(
            [
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {"type": "tool_use", "id": "u1", "name": "ws_grep", "input": {"q": 1}}
                    ],
                    "usage": {"input_tokens": 9, "output_tokens": 1},
                },
                {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 9,
                    },
                },
            ]
        )
        adapter = AnthropicBedrockAdapter(model_key=key, model_id=model_id)
        adapter._bedrock = fake
        history = [{"role": "user", "content": "go"}]
        snapshot = copy.deepcopy(history)
        turn1 = asyncio.run(
            adapter.invoke(system="sys prompt", messages=history, tools=tools, max_tokens=100)
        )
        assert [c["name"] for c in turn1.tool_calls] == ["ws_grep"], key
        # The shared history never carries a cache marker.
        assert history == snapshot, key
        history2 = [
            *history,
            {"role": "assistant", "content": turn1.assistant_blocks},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "u1", "content": "hit"}],
            },
        ]
        turn2 = asyncio.run(
            adapter.invoke(system="sys prompt", messages=history2, tools=tools, max_tokens=100)
        )
        assert turn2.usage.cache_read_tokens == 9, key
        first, second = fake.requests
        _check_claude_cached_rounds(first, second, key)


def test_caching_off_sends_no_checkpoints(monkeypatch):
    from server.agents.providers.anthropic import AnthropicBedrockAdapter

    monkeypatch.setattr(
        "server.infrastructure.feature_flags.is_enabled",
        lambda name: name != "prompt_caching",
    )
    key, model_id, _, _ = _claude_models()[0]
    fake = _FakeInvoke([_emit_payload({"answer": 1})])
    adapter = AnthropicBedrockAdapter(model_key=key, model_id=model_id)
    adapter._bedrock = fake
    asyncio.run(
        adapter.invoke(
            system="sys",
            messages=[{"role": "user", "content": "go"}],
            tools=None,
            max_tokens=50,
            force_structured=_SCHEMA,
        )
    )
    assert "cache_control" not in json.dumps(fake.requests[0])
    assert fake.requests[0]["system"] == "sys"


def test_every_gpt_adapter_parses_tool_calls(monkeypatch):
    import server.openai_bedrock.runtime as oai
    from server.agents.providers.openai import OpenAIBedrockAdapter

    tools = [{"name": "ws_grep", "description": "grep", "input_schema": {"type": "object"}}]
    for key, model_id, _, _ in _gpt_models():
        responses = _FakeResponses([_gpt_tool_round("call_9", "it_9")])
        monkeypatch.setattr(
            oai, "build_client", lambda region, r=responses: types.SimpleNamespace(responses=r)
        )
        monkeypatch.setattr(oai, "region_for", lambda model_key: "us-east-1")
        adapter = OpenAIBedrockAdapter(model_key=key, model_id=model_id)
        turn = asyncio.run(
            adapter.invoke(
                system="sys",
                messages=[{"role": "user", "content": "go"}],
                tools=tools,
                max_tokens=50,
                effort_label="extra",
            )
        )
        assert turn.stop_reason == "tool_use", key
        assert turn.tool_calls == [
            {"type": "tool_use", "id": "call_9", "name": "ws_grep", "input": {"pattern": "x"}}
        ], key
        assert turn.usage.cache_read_tokens == 30, key
        req = responses.captured[0]
        assert req["reasoning"]["effort"] in _GPT_EFFORTS, key
        assert req["tools"][0]["type"] == "function", key
