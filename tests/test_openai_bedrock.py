"""OpenAI-on-Bedrock provider: offline tests for the request mapping and the
Responses-stream -> SSE adapter (no AWS / network needed). The mapping helpers
are pure; the streaming test feeds a fake event iterator through the real
adapter and asserts the emitted SSE frames match the shared contract."""

import asyncio
import types

from server.openai_bedrock import runtime as oai
from server.openai_bedrock import stream


class FakeEvent:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeStream:
    """Async-iterable stand-in for the openai responses stream."""

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


async def _collect(agen):
    out = []
    async for x in agen:
        out.append(x)
    return out


# ── pure mapping helpers ────────────────────────────────────────────────────


def test_reasoning_effort_mapping(monkeypatch):
    # Decouple from the live config catalog: fake metas for one model whose
    # ladder tops at xhigh (5.5) and one whose ladder includes max (5.6).
    ids = {"g55": "openai.gpt-5.5", "g56": "openai.gpt-5.6-sol"}
    monkeypatch.setattr(oai, "_model_meta", lambda k: {"id": ids.get(k, "")})
    assert oai.reasoning_effort_for("g55", "none") == "none"
    assert oai.reasoning_effort_for("g55", "low") == "low"
    assert oai.reasoning_effort_for("g55", "medium") == "medium"
    assert oai.reasoning_effort_for("g55", "high") == "high"
    assert oai.reasoning_effort_for("g55", "extra") == "xhigh"
    assert oai.reasoning_effort_for("g55", "max") == "xhigh"
    assert oai.reasoning_effort_for("g55", "ultracode") == "xhigh"
    assert oai.reasoning_effort_for("g55", None) == "medium"
    assert oai.reasoning_effort_for("g55", "bogus") == "medium"
    # GPT-5.6 sends the real "max" tier for the app's top labels only.
    assert oai.reasoning_effort_for("g56", "max") == "max"
    assert oai.reasoning_effort_for("g56", "ultracode") == "max"
    assert oai.reasoning_effort_for("g56", "high") == "high"
    assert oai.reasoning_effort_for("g56", "extra") == "xhigh"
    assert oai.reasoning_effort_for("g56", None) == "medium"
    # Unknown model meta degrades to the xhigh-capped map.
    assert oai.reasoning_effort_for("nope", "max") == "xhigh"


def test_translate_tools_flat_function_shape():
    pool = [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    out = oai.translate_tools(pool)
    assert out == [
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    # Missing schema gets a safe empty-object default (Responses requires one).
    assert oai.translate_tools([{"name": "x"}])[0]["parameters"] == {
        "type": "object",
        "properties": {},
    }


def test_to_responses_input_roles_and_passthrough():
    msgs = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "there"}],
        },
        {"role": "tool", "content": "x"},  # unknown role -> coerced to user
    ]
    items = oai.to_responses_input(msgs)
    assert items[0] == {"role": "user", "content": "hello"}  # string passes through
    # list content -> typed parts (output_text for assistant)
    assert items[1] == {
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": "hi"},
            {"type": "output_text", "text": "there"},
        ],
    }
    assert items[2]["role"] == "user" and items[2]["content"] == "x"


def test_multimodal_content_maps_text_and_images():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                },
            ],
        }
    ]
    parts = oai.to_responses_input(msgs)[0]["content"]
    assert {"type": "input_text", "text": "what is this?"} in parts
    assert any(
        p["type"] == "input_image" and p["image_url"] == "data:image/png;base64,AAAA" for p in parts
    )
    # Assistant list-content stays output_text; plain strings pass through.
    assert oai.to_responses_input(
        [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
    )[0]["content"] == [{"type": "output_text", "text": "hi"}]
    assert oai.to_responses_input([{"role": "user", "content": "hello"}])[0]["content"] == "hello"


def test_tool_result_input_items_text():
    # Plain-text result -> a single function_call_output (no extra message).
    items = oai.tool_result_input_items("call_1", "done: 3 files")
    assert items == [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "done: 3 files",
        }
    ]
    # Non-str content is coerced (never crashes on odd shapes).
    assert oai.tool_result_input_items("c", 42)[0]["output"] == "42"


def test_tool_result_input_items_image_becomes_input_image():
    # A screenshot result (image block + caption) must reach GPT-5.x as a real
    # image, not a str()-ed base64 blob: caption -> function_call_output, image
    # -> a follow-up user message with an input_image part.
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "ZZZZ"}},
        {"type": "text", "text": "Screenshot of /login"},
    ]
    items = oai.tool_result_input_items("call_shot", content)
    assert len(items) == 2
    assert items[0] == {
        "type": "function_call_output",
        "call_id": "call_shot",
        "output": "Screenshot of /login",
    }
    assert items[1]["role"] == "user"
    assert {"type": "input_image", "image_url": "data:image/jpeg;base64,ZZZZ"} in items[1][
        "content"
    ]
    # Nothing in the output should carry the raw base64 payload.
    assert "ZZZZ" not in items[0]["output"]


def test_tool_result_input_items_image_without_caption_gets_pointer():
    # Responses rejects an empty function_call_output; when the tool gave no
    # text, leave a pointer so the pairing stays valid.
    items = oai.tool_result_input_items(
        "c",
        [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
            },
        ],
    )
    assert items[0]["output"]  # non-empty
    assert items[1]["role"] == "user"


def test_is_openai_model(monkeypatch):
    monkeypatch.setattr(
        oai, "_model_meta", lambda k: {"provider": "openai_bedrock"} if k == "gpt5.5" else {}
    )
    assert oai.is_openai_model("gpt5.5") is True
    assert oai.is_openai_model("opus") is False


# ── streaming event assembly ──────────────────────────────────────────────────


def test_function_call_assembly_via_stream_round():
    events = [
        FakeEvent(
            type="response.output_item.added",
            item=FakeEvent(type="function_call", id="item_1", call_id="call_abc", name="read_file"),
        ),
        FakeEvent(
            type="response.function_call_arguments.delta", item_id="item_1", delta='{"path":'
        ),
        FakeEvent(
            type="response.function_call_arguments.delta", item_id="item_1", delta='"a.txt"}'
        ),
        FakeEvent(
            type="response.function_call_arguments.done",
            item_id="item_1",
            arguments='{"path":"a.txt"}',
        ),
        FakeEvent(
            type="response.completed",
            response=FakeEvent(usage=FakeEvent(input_tokens=3, output_tokens=2)),
        ),
    ]
    fake = FakeStream(events)
    state: dict = {}
    asyncio.run(_collect(stream._stream_round(fake, state)))
    assert state["fcalls"] == [
        {"call_id": "call_abc", "name": "read_file", "args": '{"path":"a.txt"}'}
    ]
    # Exact usage from response.completed (no early-release: completed arrived).
    assert state["input_tokens"] == 3 and state["output_tokens"] == 2
    assert fake.closed is True  # stream closed even though we read to completion


def test_early_release_estimates_usage_without_completed():
    # No response.completed event (mantle's laggy tail never arrives within the
    # read). _stream_round must still finish (early-release) and estimate usage.
    events = [
        FakeEvent(type="response.output_text.delta", delta="hello world"),  # 11 chars
        FakeEvent(type="response.output_text.done"),
    ]
    state: dict = {}
    frames = asyncio.run(_collect(stream._stream_round(FakeStream(events), state)))
    assert state["fcalls"] == []
    assert state["input_tokens"] == 0  # unknown without completed
    assert state["output_tokens"] == max(1, len("hello world") // 4)  # estimated
    assert any("hello world" in f for f in frames)


def test_heartbeat_fires_during_idle_gap(monkeypatch):
    # A stream that goes idle before completing must emit SSE keepalives so the
    # browser doesn't drop the connection (the ERR_NETWORK_IO_SUSPENDED cause).
    class SlowStream:
        def __init__(self, events, gap):
            self._events, self._gap = events, gap

        def __aiter__(self):
            async def gen():
                for i, e in enumerate(self._events):
                    if i:
                        await asyncio.sleep(self._gap)
                    yield e

            return gen()

        async def close(self):
            pass

    # Tight intervals so the test is fast; grace high so early-release can't
    # preempt the gap (and there's no completed/text "ready" signal anyway).
    monkeypatch.setattr(stream, "_POLL_S", 0.02)
    monkeypatch.setattr(stream, "_HEARTBEAT_S", 0.05)
    monkeypatch.setattr(stream, "_EARLY_RELEASE_GRACE_S", 10.0)
    events = [
        FakeEvent(type="response.created"),
        FakeEvent(
            type="response.completed",
            response=FakeEvent(usage=FakeEvent(input_tokens=1, output_tokens=1)),
        ),
    ]
    state: dict = {}
    frames = asyncio.run(_collect(stream._stream_round(SlowStream(events, gap=0.3), state)))
    assert any(f.startswith(": hb") for f in frames)  # keepalive emitted during the gap


def test_stream_no_tools_emits_text_thinking_usage_done(monkeypatch):
    captured = {}

    class FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return FakeStream(
                [
                    FakeEvent(type="response.reasoning_summary_text.delta", delta="let me think"),
                    FakeEvent(type="response.output_text.delta", delta="Hello "),
                    FakeEvent(type="response.output_text.delta", delta="world"),
                    FakeEvent(
                        type="response.completed",
                        response=FakeEvent(usage=FakeEvent(input_tokens=11, output_tokens=4)),
                    ),
                ]
            )

    fake_client = types.SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(stream.oai, "build_client", lambda region: fake_client)
    monkeypatch.setattr(stream.oai, "region_for", lambda mk: "us-east-2")

    frames = asyncio.run(
        _collect(
            stream.stream_openai_chat(
                model_key="gpt5.5",
                model_id="openai.gpt-5.5",
                system_prompt="sys",
                messages=[{"role": "user", "content": "hi"}],
                session_id="t-sess",
                effort="medium",
                verbosity="low",
                tool_ctx=None,
            )
        )
    )
    joined = "".join(frames)

    # Reasoning summary -> thinking_* channel, then visible text, then usage + DONE.
    assert "thinking_start" in joined
    assert "let me think" in joined
    assert "thinking_stop" in joined
    assert "Hello " in joined and "world" in joined
    assert "usage" in joined
    assert frames[-1] == "data: [DONE]\n\n"

    # Request was shaped correctly for GPT-5.x.
    assert captured["model"] == "openai.gpt-5.5"
    assert captured["instructions"].startswith("sys")  # + GPT literal-interpretation nudge
    assert captured["reasoning"] == {"effort": "medium", "summary": "auto"}
    assert captured["text"] == {"verbosity": "low"}
    assert captured["store"] is False
    assert captured["stream"] is True
    assert "tools" not in captured  # tool_ctx=None => no tools advertised


# --- Region resolution: driven by config, no hardcoded default ---


def test_region_for_uses_bedrock_region_when_no_override(monkeypatch):
    # No per-model override => same account-wide bedrock_region the Anthropic
    # path uses. Whatever config says is what we get — no us-east-2 fallback.
    monkeypatch.setattr(oai, "_model_meta", lambda k: {"provider": "openai_bedrock"})
    monkeypatch.setattr(oai, "load_config", lambda: {"bedrock_region": "eu-west-1"})
    assert oai.region_for("gpt5.5") == "eu-west-1"


def test_region_for_openai_region_override_wins(monkeypatch):
    monkeypatch.setattr(oai, "_model_meta", lambda k: {"openai_region": "us-west-2"})
    monkeypatch.setattr(oai, "load_config", lambda: {"bedrock_region": "us-east-1"})
    assert oai.region_for("gpt5.4") == "us-west-2"


def test_region_for_blank_override_falls_back_to_bedrock_region(monkeypatch):
    # A whitespace-only override must not produce region="" — fall back cleanly.
    monkeypatch.setattr(oai, "_model_meta", lambda k: {"openai_region": "   "})
    monkeypatch.setattr(oai, "load_config", lambda: {"bedrock_region": "us-east-1"})
    assert oai.region_for("gpt5.5") == "us-east-1"


def test_region_for_has_no_hardcoded_default(monkeypatch):
    # An arbitrary configured region flows straight through — proves there is no
    # baked-in us-east-2 masking the config value.
    monkeypatch.setattr(oai, "_model_meta", lambda k: {})
    monkeypatch.setattr(oai, "load_config", lambda: {"bedrock_region": "ap-southeast-2"})
    assert oai.region_for("gpt5.5") == "ap-southeast-2"


# --- Config propagation: a GPT model is as turnkey as a Claude one ---


def test_provider_inferred_from_openai_id():
    # Minimal entry (id + label only) — provider, effort tier, and verbosity are
    # all inferred, and no region is pinned so it follows bedrock_region.
    from server.infrastructure.config import _normalize_chat_models

    _ids, meta = _normalize_chat_models(
        {"gpt5.6": {"id": "openai.gpt-5.6-sol", "label": "GPT-5.6"}}
    )
    m = meta["gpt5.6"]
    # provider is what is_openai_model() keys off — inference alone makes it route.
    assert m["provider"] == "openai_bedrock"
    assert m["effort_tier"] == "openai"
    assert m["verbosity"] == "medium"
    assert m["openai_region"] is None


def test_explicit_provider_overrides_id_inference():
    from server.infrastructure.config import _normalize_chat_models

    _ids, meta = _normalize_chat_models({"weird": {"id": "openai.custom", "provider": "anthropic"}})
    assert meta["weird"]["provider"] == "anthropic"
    # Non-OpenAI provider => effort tier inferred from the key, not the openai ladder.
    assert meta["weird"]["effort_tier"] != "openai"


def test_anthropic_id_stays_anthropic():
    from server.infrastructure.config import _normalize_chat_models

    _ids, meta = _normalize_chat_models(
        {"opus4.8": {"id": "global.anthropic.claude-opus-4-8", "label": "Opus 4.8"}}
    )
    assert meta["opus4.8"]["provider"] == "anthropic"


# --- Transcript threading through tool_ctx (PR #135 follow-up) ---


def test_openai_summarize_transcript_gets_transcript_from_tool_ctx(monkeypatch):
    """Regression: the OpenAI-on-Bedrock path must thread the request transcript
    through ``tool_ctx`` into ``run_tool_round`` so the real
    ``summarize_transcript`` executor sees it. Before the fix, tool_ctx carried
    no transcript, ``run_tool_round`` defaulted it to "", and exec_summarize
    returned "No transcript available to summarize." for every GPT-5.x turn.

    Drives the FULL real pipeline (no run_tool_round mock): round 1 calls
    summarize_transcript, round 2 answers, so the assertion covers the whole
    tool_ctx -> run_tool_round -> execute_tool_batch -> exec_summarize chain."""
    import server.executors.content  # noqa: F401 — registers summarize_transcript
    import server.skills as sk

    sk.SKILLS = sk.load_skills()

    rounds = [
        [  # round 1: model calls summarize_transcript
            FakeEvent(
                type="response.output_item.added",
                item=FakeEvent(
                    type="function_call",
                    id="item_1",
                    call_id="call_sum",
                    name="summarize_transcript",
                ),
            ),
            FakeEvent(
                type="response.function_call_arguments.done",
                item_id="item_1",
                arguments='{"style":"brief"}',
            ),
            FakeEvent(
                type="response.completed",
                response=FakeEvent(usage=FakeEvent(input_tokens=5, output_tokens=2)),
            ),
        ],
        [  # round 2: model answers using the tool result
            FakeEvent(type="response.output_text.delta", delta="Here is the summary."),
            FakeEvent(
                type="response.completed",
                response=FakeEvent(usage=FakeEvent(input_tokens=6, output_tokens=3)),
            ),
        ],
    ]

    class FakeResponses:
        async def create(self, **kwargs):
            return FakeStream(rounds.pop(0))

    fake_client = types.SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(stream.oai, "build_client", lambda region: fake_client)
    monkeypatch.setattr(stream.oai, "region_for", lambda mk: "us-east-2")

    transcript = "Alice: We ship on Friday. Bob: Agreed, code freeze is Thursday."
    frames = asyncio.run(
        _collect(
            stream.stream_openai_chat(
                model_key="gpt5.5",
                model_id="openai.gpt-5.5",
                system_prompt="sys",
                messages=[{"role": "user", "content": "summarize"}],
                session_id="t-sess",
                effort="medium",
                verbosity="low",
                tool_ctx={"transcript": transcript},
            )
        )
    )
    joined = "".join(frames)
    assert "No transcript available" not in joined
    # The real transcript reached exec_summarize and came back in the result.
    assert "We ship on Friday" in joined
    assert frames[-1] == "data: [DONE]\n\n"


# --- Final-round forced answer (parity with the cloud path) ---


def test_last_round_forces_answer_with_tool_choice_none(monkeypatch):
    """A model that wants a tool every round must still end the turn with a
    text answer: the final round sends tool_choice="none" (mirroring the cloud
    path's no-tools last round), so the round-limit apology never fires."""
    calls: list[dict] = []

    def _fcall_stream():
        return FakeStream(
            [
                FakeEvent(
                    type="response.output_item.added",
                    item=FakeEvent(type="function_call", id="i1", call_id="c1", name="probe"),
                ),
                FakeEvent(
                    type="response.function_call_arguments.done", item_id="i1", arguments="{}"
                ),
                FakeEvent(
                    type="response.completed",
                    response=FakeEvent(usage=FakeEvent(input_tokens=5, output_tokens=2)),
                ),
            ]
        )

    def _text_stream():
        return FakeStream(
            [
                FakeEvent(type="response.output_text.delta", delta="Best answer from evidence."),
                FakeEvent(
                    type="response.completed",
                    response=FakeEvent(usage=FakeEvent(input_tokens=5, output_tokens=3)),
                ),
            ]
        )

    class FakeResponses:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("tool_choice") == "none":
                return _text_stream()
            return _fcall_stream()  # the model would tool-loop forever

    async def fake_run_tool_round(tool_uses, **kw):
        results = [
            {"type": "tool_result", "tool_use_id": tu["id"], "content": "big output"}
            for tu in tool_uses
        ]
        return results, [], False, False

    monkeypatch.setattr(
        stream.oai, "build_client", lambda region: types.SimpleNamespace(responses=FakeResponses())
    )
    monkeypatch.setattr(stream.oai, "region_for", lambda mk: "us-east-2")
    monkeypatch.setattr(
        stream,
        "_assemble_tools",
        lambda tc: [{"type": "function", "name": "probe", "parameters": {}}],
    )
    monkeypatch.setattr(stream, "run_tool_round", fake_run_tool_round)
    monkeypatch.setattr(stream, "_MAX_TOOL_ROUNDS", 3)

    frames = asyncio.run(
        _collect(
            stream._tool_loop(
                "gpt5.5",
                "openai.gpt-5.5",
                "sys",
                [{"role": "user", "content": "hi"}],
                {"plan_mode": False},
                session_id="t-sess",
                effort="medium",
                verbosity="low",
            )
        )
    )
    joined = "".join(frames)

    assert len(calls) == 3
    assert [c.get("tool_choice") for c in calls] == ["auto", "auto", "none"]
    assert "Best answer from evidence." in joined
    assert "Reached the tool-call round limit" not in joined
    assert frames[-1] == "data: [DONE]\n\n"


def test_memory_hooks_skipped_on_pause_fired_on_completion(monkeypatch):
    # Regression: the post-turn memory hooks must NOT fire when the OpenAI turn
    # pauses for an approval (they'd run on a half-finished turn and never on the
    # resumed remainder); they fire only when the turn actually completes.
    calls = []
    monkeypatch.setattr(stream, "_spawn_memory_hooks", lambda *a, **k: calls.append(a))
    stream._openai_paused.pop("t-pause", None)

    async def _fake_pause(*a, **k):
        # Simulate _tool_loop stashing a pause, exactly as the real pause path does.
        stream._openai_paused[k["session_id"]] = {"memory_ctx": None}
        yield "data: [DONE]\n\n"

    async def _fake_done(*a, **k):
        yield 'data: {"text": "hi"}\n\n'
        yield "data: [DONE]\n\n"

    # Paused turn: hooks must be skipped.
    monkeypatch.setattr(stream, "_tool_loop", _fake_pause)
    asyncio.run(
        _collect(
            stream.stream_openai_chat(
                model_key="gpt5.5",
                model_id="openai.gpt-5.5",
                system_prompt="s",
                messages=[{"role": "user", "content": "hi"}],
                session_id="t-pause",
                effort="medium",
                verbosity="low",
                tool_ctx=None,
            )
        )
    )
    assert calls == [], "memory hooks must not fire on a pause"
    stream._openai_paused.pop("t-pause", None)

    # Completed turn: hooks fire once.
    monkeypatch.setattr(stream, "_tool_loop", _fake_done)
    asyncio.run(
        _collect(
            stream.stream_openai_chat(
                model_key="gpt5.5",
                model_id="openai.gpt-5.5",
                system_prompt="s",
                messages=[{"role": "user", "content": "hi"}],
                session_id="t-done",
                effort="medium",
                verbosity="low",
                tool_ctx=None,
            )
        )
    )
    assert len(calls) == 1, "memory hooks must fire once on completion"
