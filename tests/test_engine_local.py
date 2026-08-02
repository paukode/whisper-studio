"""The local (llama-server) adapter on the unified turn engine (offline).

Ports the loop coverage from the pre-engine local path: thinking/text
normalization, tool rounds through the shared safety gate, approval pauses via
the unified store, argument coercion before dispatch, the empty-turn message,
token accounting (cached-prefix semantics), and the offline invariants (no
Bedrock client, empty tool-executor model id).
"""

import asyncio
import json

import server.local.server_stream as SS
from server.chat.engine.local import LocalAdapter
from server.chat.engine.pause import paused_sessions
from server.chat.engine.policy import LOCAL_POLICY
from server.chat.engine.runner import TurnContext, run_turn


def _fake_rounds(*rounds):
    """Stand-in for SS._stream_round: each round is (pieces, calls[, usage])."""
    box = {"i": 0}

    async def gen(base_url, payload):
        gen.payloads.append(payload)
        i = min(box["i"], len(rounds) - 1)
        box["i"] = i + 1
        pieces, calls, *rest = rounds[i]
        for kind, piece in pieces:
            yield (kind, piece)
        yield (
            "done",
            {
                "calls": calls,
                "finish_reason": "tool_calls" if calls else "stop",
                "usage": rest[0] if rest else {},
            },
        )

    gen.payloads = []
    return gen


def _fake_tool_pipeline(monkeypatch, results_by_id=None, pending=False, capture=None):
    """Fake execute_tool_batch/process_tool_results with the given outcomes."""
    import server.tool_executor as TE

    async def fake_batch(tool_uses, **kw):
        if capture is not None:
            capture["tool_uses"] = tool_uses
            capture["batch_kwargs"] = kw
        return list(tool_uses)

    async def fake_process(states, budget_fn, **kw):
        if capture is not None:
            capture["process_kwargs"] = kw
        results = [
            {
                "type": "tool_result",
                "tool_use_id": t["id"],
                "content": (results_by_id or {}).get(t["id"], "ok"),
            }
            for t in states
        ]
        return (results, [], pending, False)

    monkeypatch.setattr(TE, "execute_tool_batch", fake_batch)
    monkeypatch.setattr(TE, "process_tool_results", fake_process)


def _run_local_turn(
    monkeypatch,
    rounds,
    *,
    messages=None,
    session_id="local-t",
    tools_enabled=False,
    thinking=False,
    **pipeline_kw,
):
    fake = _fake_rounds(*rounds)
    monkeypatch.setattr(SS, "_stream_round", fake)
    _fake_tool_pipeline(monkeypatch, **pipeline_kw)
    adapter = LocalAdapter(
        model_key="local_gemma",
        base_url="http://127.0.0.1:9999",
        system_prompt="sys",
        thinking=thinking,
        tools_enabled=tools_enabled,
    )
    ctx = TurnContext(
        session_id=session_id,
        model_key="local_gemma",
        model_id="local_gemma",
        messages=messages or [{"role": "user", "content": "hi"}],
        adapter=adapter,
        policy=LOCAL_POLICY,
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return [c async for c in run_turn(ctx)]

    return "".join(asyncio.run(go())), fake


def test_reasoning_becomes_thinking_events(monkeypatch):
    blob, _ = _run_local_turn(
        monkeypatch, [([("thinking", "hmm"), ("text", "answer")], [])], thinking=True
    )
    assert '"thinking_start": true' in blob
    assert '"thinking": "hmm"' in blob
    assert '"thinking_stop": true' in blob
    assert '"text": "answer"' in blob
    assert blob.index('"thinking_stop"') < blob.index('"text": "answer"')
    assert blob.strip().endswith("data: [DONE]")


def test_thinking_stop_emitted_when_round_has_no_answer_text(monkeypatch):
    blob, _ = _run_local_turn(monkeypatch, [([("thinking", "only")], [])])
    assert '"thinking_start": true' in blob and '"thinking_stop": true' in blob


def test_tool_round_flows_the_shared_gate_and_feeds_results_back(monkeypatch):
    calls = [{"id": "c1", "name": "ws_read_file", "input": {"path": "a.py"}}]
    capture: dict = {}
    blob, fake = _run_local_turn(
        monkeypatch,
        [([], calls), ([("text", "done")], [])],
        tools_enabled=True,
        results_by_id={"c1": "print(1)"},
        capture=capture,
    )
    assert '"skill": "ws_read_file"' in blob
    assert '"path": "a.py"' in blob
    assert '"text": "done"' in blob
    # Offline invariant: the tool pipeline got the EMPTY model id (permission
    # explainer / auto-mode classifier stay off).
    assert capture["batch_kwargs"]["model_id"] == ""
    assert capture["process_kwargs"]["model_id"] == ""
    # Round 2's payload carries the native tool traffic (assistant tool_calls
    # + role:tool result), not user-folded text.
    round2 = fake.payloads[1]["messages"]
    assistant = [m for m in round2 if m.get("role") == "assistant" and m.get("tool_calls")]
    assert assistant and assistant[0]["tool_calls"][0]["function"]["name"] == "ws_read_file"
    tool_msgs = [m for m in round2 if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"
    assert tool_msgs[0]["content"] == "print(1)"


def test_parallel_calls_execute_in_one_round(monkeypatch):
    calls = [
        {"id": "a", "name": "ws_read_file", "input": {"path": "x"}},
        {"id": "b", "name": "ws_read_file", "input": {"path": "y"}},
    ]
    capture: dict = {}
    _run_local_turn(
        monkeypatch,
        [([], calls), ([("text", "ok")], [])],
        tools_enabled=True,
        capture=capture,
    )
    assert len(capture["tool_uses"]) == 2


def test_pause_on_approval_stashes_unified_state(monkeypatch):
    calls = [{"id": "w1", "name": "ws_write_file", "input": {"path": "a.txt"}}]
    paused_sessions.pop("local-pause", None)
    blob, _ = _run_local_turn(
        monkeypatch,
        [([], calls), ([("text", "after")], [])],
        session_id="local-pause",
        tools_enabled=True,
        results_by_id={"w1": "[Not executed]"},
        pending=True,
    )
    assert '"text"' not in blob  # the destructive action did NOT run
    assert "local-pause" in paused_sessions
    assert paused_sessions["local-pause"]["provider"] == "local"
    # The stashed canonical messages end with the assistant tool_use turn.
    stashed = paused_sessions.pop("local-pause")
    assert stashed["messages"][-1]["content"][-1]["type"] == "tool_use"
    assert blob.strip().endswith("data: [DONE]")


def test_last_round_payload_forbids_tools(monkeypatch):
    adapter = LocalAdapter(
        model_key="k",
        base_url="http://x",
        system_prompt="",
        thinking=False,
        tools_enabled=True,
    )
    fake = _fake_rounds(([("text", "final")], []))
    monkeypatch.setattr(SS, "_stream_round", fake)
    tools = [{"name": "t", "description": "", "input_schema": {}}]

    async def go():
        return [
            e
            async for e in adapter.stream_round(
                [{"role": "user", "content": "q"}], tools, None, 49, True
            )
        ]

    asyncio.run(go())
    assert fake.payloads[0]["tool_choice"] == "none"


def test_adapter_coerces_loose_argument_types(monkeypatch):
    adapter = LocalAdapter(
        model_key="k", base_url="http://x", system_prompt="", thinking=False, tools_enabled=True
    )
    fake = _fake_rounds(([], [{"id": "c", "name": "t", "input": {"n": "5", "b": "true"}}]))
    monkeypatch.setattr(SS, "_stream_round", fake)
    tools = [
        {
            "name": "t",
            "description": "",
            "input_schema": {
                "type": "object",
                "properties": {"n": {"type": "integer"}, "b": {"type": "boolean"}},
            },
        }
    ]

    async def go():
        return [
            e
            async for e in adapter.stream_round(
                [{"role": "user", "content": "q"}], tools, None, 0, False
            )
        ]

    evs = asyncio.run(go())
    from server.chat.engine.events import ToolCall

    call = next(e for e in evs if isinstance(e, ToolCall))
    assert call.input == {"n": 5, "b": True}


def test_empty_turn_gets_an_explanatory_line(monkeypatch):
    blob, _ = _run_local_turn(monkeypatch, [([], [])])
    assert "No answer: the model ended its turn without producing any text" in blob


def test_no_fallback_line_when_the_turn_produced_text(monkeypatch):
    blob, _ = _run_local_turn(monkeypatch, [([("text", "real answer")], [])])
    assert "No answer" not in blob


def test_context_overflow_raises_prompt_too_long_and_salvages(monkeypatch):
    calls_round = {"n": 0}

    async def overflow_then_answer(base_url, payload):
        calls_round["n"] += 1
        if calls_round["n"] == 1:
            raise RuntimeError(
                "the request exceeds the available context size (exceed_context_size)"
            )
        yield ("text", "Salvaged locally.")
        yield ("done", {"calls": [], "finish_reason": "stop", "usage": {}})

    monkeypatch.setattr(SS, "_stream_round", overflow_then_answer)
    _fake_tool_pipeline(monkeypatch)
    adapter = LocalAdapter(
        model_key="local_gemma",
        base_url="http://x",
        system_prompt="s",
        thinking=False,
        tools_enabled=False,
    )
    ctx = TurnContext(
        session_id="local-ptl",
        model_key="local_gemma",
        model_id="local_gemma",
        messages=[{"role": "user", "content": "big"}],
        adapter=adapter,
        policy=LOCAL_POLICY,
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return [c async for c in run_turn(ctx)]

    blob = "".join(asyncio.run(go()))
    assert "synthesizing a final answer" in blob
    assert "Salvaged locally." in blob


def test_usage_frame_reports_cached_prefix_semantics(monkeypatch):
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 40,
        "prompt_tokens_details": {"cached_tokens": 900},
    }
    blob, _ = _run_local_turn(monkeypatch, [([("text", "hi")], [], usage)])
    frames = [
        json.loads(ln[6:])["usage"]
        for ln in blob.splitlines()
        if ln.startswith("data: ") and '"usage"' in ln
    ]
    assert frames[0]["input_tokens"] == 100  # prompt minus cached prefix
    assert frames[0]["cache_read_tokens"] == 900
    assert frames[0]["output_tokens"] == 40
    assert frames[0]["context_used"] == 1000  # full prompt: real window fill
    assert frames[0]["estimated_cost_usd"] == 0.0


def test_usage_falls_back_to_char_estimate_without_usage(monkeypatch):
    blob, _ = _run_local_turn(monkeypatch, [([("text", "x" * 80)], [])])
    frames = [
        json.loads(ln[6:])["usage"]
        for ln in blob.splitlines()
        if ln.startswith("data: ") and '"usage"' in ln
    ]
    assert frames[0]["input_tokens"] == 0
    assert frames[0]["output_tokens"] == 20  # 80 chars / 4


def test_transport_error_surfaces_and_ends(monkeypatch):
    async def boom(base_url, payload):
        raise RuntimeError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(SS, "_stream_round", boom)
    _fake_tool_pipeline(monkeypatch)
    adapter = LocalAdapter(
        model_key="k", base_url="http://x", system_prompt="", thinking=False, tools_enabled=False
    )
    ctx = TurnContext(
        session_id="local-err",
        model_key="k",
        model_id="k",
        messages=[{"role": "user", "content": "q"}],
        adapter=adapter,
        policy=LOCAL_POLICY,
        loop=None,
        executor=None,
        tool_exec_model_id="",
        memory_hooks=lambda msgs: None,
    )

    async def go():
        return [c async for c in run_turn(ctx)]

    blob = "".join(asyncio.run(go()))
    assert "connection refused" in blob
    assert blob.strip().endswith("data: [DONE]")


def test_conversion_flattens_blocks_and_keeps_tool_fidelity():
    adapter = LocalAdapter(
        model_key="k", base_url="http://x", system_prompt="SYS", thinking=False, tools_enabled=True
    )
    msgs = adapter.to_openai_messages(
        [
            {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Reading."},
                    {"type": "tool_use", "id": "c1", "name": "read", "input": {"p": 1}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "body"}],
            },
        ]
    )
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "look"}  # image dropped
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "read"
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "body"}
