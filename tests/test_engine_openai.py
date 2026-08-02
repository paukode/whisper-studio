"""The OpenAI Responses adapter on the unified turn engine (offline).

Ports the coverage the old openai_bedrock/stream.py tests provided, now
against the engine: stream-event assembly, early release + heartbeat (mantle
quirks), canonical-message conversion fidelity (function_call linkage),
last-round tool lockout, prompt-too-long rescue/salvage, and the end-to-end
engine turn with a fake AsyncOpenAI client.
"""

import asyncio
import json
import types

from server.chat.engine import events as EV
from server.chat.engine.openai import OpenAIResponsesAdapter


class FakeEvent:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeStream:
    def __init__(self, events):
        self._events = events
        self.closed = False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e

        return gen()

    async def close(self):
        self.closed = True


class FakeResponses:
    """Scripted responses.create: one FakeStream per round, kwargs captured."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.captured: list[dict] = []

    async def create(self, **kwargs):
        self.captured.append(kwargs)
        if not self.rounds:
            raise RuntimeError("FakeResponses: no scripted rounds left")
        nxt = self.rounds.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return FakeStream(nxt)


def _adapter(monkeypatch, rounds, model_key="gpt5.5"):
    import server.openai_bedrock.runtime as oai

    fr = FakeResponses(rounds)
    fake_client = types.SimpleNamespace(responses=fr)
    monkeypatch.setattr(oai, "build_client", lambda region: fake_client)
    monkeypatch.setattr(oai, "region_for", lambda mk: "us-east-2")
    a = OpenAIResponsesAdapter(
        model_key=model_key,
        model_id="openai.gpt-5.5",
        system_prompt="sys",
        effort_label="high",
        session_id="t-sess",
    )
    return a, fr


async def _collect(agen):
    out = []
    async for x in agen:
        out.append(x)
    return out


def _completed(in_tok=3, out_tok=2):
    return FakeEvent(
        type="response.completed",
        response=FakeEvent(usage=FakeEvent(input_tokens=in_tok, output_tokens=out_tok)),
    )


# ── stream-event assembly ─────────────────────────────────────────────────────


def test_round_assembles_text_thinking_and_usage(monkeypatch):
    a, _ = _adapter(
        monkeypatch,
        [
            [
                FakeEvent(type="response.reasoning_summary_text.delta", delta="let me think"),
                FakeEvent(type="response.output_text.delta", delta="Hello "),
                FakeEvent(type="response.output_text.delta", delta="world"),
                _completed(11, 4),
            ]
        ],
    )
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "hi"}], [], None, 0, False))
    )
    kinds = [type(e).__name__ for e in evs]
    assert kinds[:4] == ["ThinkingStart", "ThinkingDelta", "ThinkingStop", "TextDelta"]
    result = evs[-1]
    assert isinstance(result, EV.RoundResult)
    assert result.stop_reason == "end_turn"
    assert result.content == [{"type": "text", "text": "Hello world"}]
    assert result.usage.input_tokens == 11 and result.usage.exact is True


def test_function_call_assembly_and_stop_reason(monkeypatch):
    a, fr = _adapter(
        monkeypatch,
        [
            [
                FakeEvent(
                    type="response.output_item.added",
                    item=FakeEvent(
                        type="function_call", id="item_1", call_id="call_abc", name="read_file"
                    ),
                ),
                FakeEvent(
                    type="response.function_call_arguments.delta",
                    item_id="item_1",
                    delta='{"path":',
                ),
                FakeEvent(
                    type="response.function_call_arguments.delta",
                    item_id="item_1",
                    delta='"a.txt"}',
                ),
                FakeEvent(
                    type="response.function_call_arguments.done",
                    item_id="item_1",
                    arguments='{"path":"a.txt"}',
                ),
                _completed(),
            ]
        ],
    )
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "go"}], [], None, 0, False))
    )
    starts = [e for e in evs if isinstance(e, EV.ToolCallStart)]
    calls = [e for e in evs if isinstance(e, EV.ToolCall)]
    assert starts[0].name == "read_file"
    assert calls[0].id == "call_abc" and calls[0].input == {"path": "a.txt"}
    result = evs[-1]
    assert result.stop_reason == "tool_use"
    assert result.content[-1] == {
        "type": "tool_use",
        "id": "call_abc",
        "name": "read_file",
        "input": {"path": "a.txt"},
    }


def test_early_release_estimates_usage_without_completed(monkeypatch):
    a, _ = _adapter(
        monkeypatch,
        [
            [
                FakeEvent(type="response.output_text.delta", delta="hello world"),
                FakeEvent(type="response.output_text.done"),
            ]
        ],
    )
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "hi"}], [], None, 0, False))
    )
    result = evs[-1]
    assert isinstance(result, EV.RoundResult)
    assert result.usage.exact is False
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == max(1, len("hello world") // 4)


def test_heartbeat_fires_during_idle_gap(monkeypatch):
    import server.chat.engine.openai as mod

    class SlowStream(FakeStream):
        def __aiter__(self):
            async def gen():
                yield FakeEvent(type="response.created")
                await asyncio.sleep(0.3)
                yield _completed(1, 1)

            return gen()

    monkeypatch.setattr(mod, "_POLL_S", 0.02)
    monkeypatch.setattr(mod, "_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(mod, "_EARLY_RELEASE_GRACE_S", 10.0)

    import server.openai_bedrock.runtime as oai

    class FR:
        async def create(self, **kw):
            return SlowStream([])

    monkeypatch.setattr(oai, "build_client", lambda region: types.SimpleNamespace(responses=FR()))
    monkeypatch.setattr(oai, "region_for", lambda mk: "us-east-2")
    a = OpenAIResponsesAdapter(
        model_key="gpt5.5", model_id="openai.gpt-5.5", system_prompt="s", effort_label=None
    )
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "hi"}], [], None, 0, False))
    )
    assert any(isinstance(e, EV.Heartbeat) for e in evs)


# ── canonical → Responses input conversion ───────────────────────────────────


def test_conversion_preserves_function_call_linkage(monkeypatch):
    a, _ = _adapter(monkeypatch, [])
    messages = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "s"},
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"p": 1}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "file body"}],
        },
    ]
    items = a.to_input_items(messages)
    kinds = [(i.get("type") or i.get("role")) for i in items]
    assert kinds == ["user", "assistant", "function_call", "function_call_output"]
    fc = items[2]
    assert fc["call_id"] == "call_1" and json.loads(fc["arguments"]) == {"p": 1}
    assert items[3]["call_id"] == "call_1" and items[3]["output"] == "file body"
    # thinking blocks are not replayable and must not leak into the payload.
    assert "hmm" not in json.dumps(items)


def test_last_round_keeps_tools_with_choice_none(monkeypatch):
    a, fr = _adapter(
        monkeypatch,
        [[FakeEvent(type="response.output_text.delta", delta="final"), _completed()]],
    )
    tools = [{"name": "web_search", "description": "d", "input_schema": {}}]
    asyncio.run(_collect(a.stream_round([{"role": "user", "content": "x"}], tools, None, 49, True)))
    req = fr.captured[0]
    assert req["tool_choice"] == "none"
    assert req["tools"], "tools stay in the request for prefix-cache stability"


def test_excluded_tools_are_dropped(monkeypatch):
    a, fr = _adapter(
        monkeypatch,
        [[FakeEvent(type="response.output_text.delta", delta="ok"), _completed()]],
    )
    tools = [
        {"name": "sleep", "description": "d", "input_schema": {}},
        {"name": "web_search", "description": "d", "input_schema": {}},
    ]
    asyncio.run(_collect(a.stream_round([{"role": "user", "content": "x"}], tools, None, 0, False)))
    names = [t["name"] for t in fr.captured[0]["tools"]]
    assert "sleep" not in names and "web_search" in names


# ── prompt-too-long classification ────────────────────────────────────────────


def test_mantle_rejection_raises_prompt_too_long(monkeypatch):
    from server.infrastructure.errors import PromptTooLongError

    a, _ = _adapter(
        monkeypatch,
        [
            Exception(
                "prompt tokens (281096) exceed customer model maximum (278528) "
                "for 7903dfda-4910-4bc7-86ca-ea247ecf6bd2"
            )
        ],
    )

    async def run():
        try:
            async for _ in a.stream_round([{"role": "user", "content": "big"}], [], None, 0, False):
                pass
        except PromptTooLongError:
            return True
        return False

    assert asyncio.run(run()) is True


# ── end-to-end: GPT turn through the engine + chat route ─────────────────────


def _gpt_turn(monkeypatch, rounds, body=None):
    import server.openai_bedrock.runtime as oai
    from tests.golden_harness import run_chat_turn

    fr = FakeResponses(rounds)
    fake_client = types.SimpleNamespace(responses=fr)
    monkeypatch.setattr(oai, "build_client", lambda region: fake_client)
    monkeypatch.setattr(oai, "region_for", lambda mk: "us-east-2")

    class _NoBedrock:
        def __getattr__(self, name):
            raise AssertionError("GPT turn must not touch Bedrock Anthropic")

    lines = run_chat_turn(monkeypatch, _NoBedrock(), {"model": "gpt5.6-sol", **(body or {})})
    return lines, fr


def test_gpt_turn_streams_through_engine(monkeypatch):
    lines, fr = _gpt_turn(
        monkeypatch,
        [
            [
                FakeEvent(type="response.reasoning_summary_text.delta", delta="think"),
                FakeEvent(type="response.output_text.delta", delta="GPT answer"),
                _completed(20, 5),
            ]
        ],
    )
    joined = "\n".join(lines)
    assert "thinking_start" in joined and "GPT answer" in joined
    assert '"context_max": 278528' in joined  # window metadata reached the meter
    assert lines[-1] == "[DONE]"
    assert fr.captured[0]["prompt_cache_key"] == "ws-golden-session"
    assert fr.captured[0]["instructions"].endswith("them.")  # GPT suffix appended


def test_gpt_tool_round_through_shared_executor(monkeypatch):
    # Round 1 calls analyze_document (offline-deterministic result), round 2
    # answers — through the SAME engine loop and safety gate Claude uses.
    lines, fr = _gpt_turn(
        monkeypatch,
        [
            [
                FakeEvent(
                    type="response.output_item.added",
                    item=FakeEvent(
                        type="function_call", id="i1", call_id="c1", name="analyze_document"
                    ),
                ),
                FakeEvent(
                    type="response.function_call_arguments.done",
                    item_id="i1",
                    arguments='{"filename": "x.md", "question": "?"}',
                ),
                _completed(),
            ],
            [FakeEvent(type="response.output_text.delta", delta="Nothing attached."), _completed()],
        ],
    )
    joined = "\n".join(lines)
    assert '"skill": "analyze_document"' in joined
    assert "No documents attached." in joined  # the real executor ran
    assert "Nothing attached." in joined
    # Round 2 request must carry the function_call + function_call_output pair.
    round2_items = fr.captured[1]["input"]
    types_ = [(i.get("type") or i.get("role")) for i in round2_items]
    assert "function_call" in types_ and "function_call_output" in types_


def test_gpt_prompt_too_long_salvages(monkeypatch):
    lines, fr = _gpt_turn(
        monkeypatch,
        [
            Exception("prompt tokens (281096) exceed customer model maximum (278528)"),
            [FakeEvent(type="response.output_text.delta", delta="Salvaged."), _completed()],
        ],
    )
    joined = "\n".join(lines)
    assert "synthesizing a final answer" in joined
    assert "Salvaged." in joined
    assert lines[-1] == "[DONE]"
    # The salvage round went out without tools.
    assert "tools" not in fr.captured[-1]
