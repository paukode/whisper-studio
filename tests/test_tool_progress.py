"""Tool-argument streaming progress: a whole HTML app arrives as one tool
call's arguments and can take minutes; both adapters now report the size
received every TOOL_ARGS_PROGRESS_STEP chars and the engine forwards it as a
``skill_progress`` frame. Small calls stay frame-free (golden fixtures pin
the frame vocabulary for those)."""

from __future__ import annotations

import asyncio
import json
import time

from server.chat.engine import events as EV
from tests.golden_harness import (
    FakeBedrockClient,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
    tool_use_block,
)
from tests.test_engine_openai import FakeEvent, _adapter, _collect, _completed

STEP = EV.TOOL_ARGS_PROGRESS_STEP


def PAUSE():
    """The silence while the model writes an argument the API holds back."""
    time.sleep(0.4)


def _fc_round(deltas: list[str]):
    events = [
        FakeEvent(
            type="response.output_item.added",
            item=FakeEvent(
                type="function_call", id="item_1", call_id="call_1", name="create_artifact"
            ),
        )
    ]
    for d in deltas:
        events.append(
            FakeEvent(type="response.function_call_arguments.delta", item_id="item_1", delta=d)
        )
    joined = "".join(deltas)
    events.append(
        FakeEvent(type="response.function_call_arguments.done", item_id="item_1", arguments=joined)
    )
    events.append(_completed())
    return events


def test_openai_adapter_reports_progress_per_step(monkeypatch):
    given = {"title": "Big", "html": "<!DOCTYPE html><p>" + "x" * (3 * STEP) + "</p>"}
    encoded = json.dumps(given)
    chunks = [encoded[i : i + 3000] for i in range(0, len(encoded), 3000)]
    a, _fr = _adapter(monkeypatch, [_fc_round(chunks)])
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "go"}], [], None, 0, False))
    )
    progress = [e for e in evs if isinstance(e, EV.ToolCallProgress)]
    assert progress, "expected progress events for a large tool call"
    assert all(p.name == "create_artifact" for p in progress)
    chars = [p.chars for p in progress]
    assert chars == sorted(chars) and chars[0] >= STEP
    # One report per crossed step, never per delta.
    total = len(encoded)
    assert len(progress) <= total // STEP + 1
    (call,) = [e for e in evs if isinstance(e, EV.ToolCall)]
    assert call.input == given
    assert isinstance(evs[-1], EV.RoundResult)


def test_openai_adapter_stays_silent_for_small_calls(monkeypatch):
    a, _fr = _adapter(monkeypatch, [_fc_round(['{"path":', '"a.txt"}'])])
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "go"}], [], None, 0, False))
    )
    assert not [e for e in evs if isinstance(e, EV.ToolCallProgress)]


def test_engine_forwards_progress_as_skill_progress_frames(monkeypatch):
    big_html = "<!DOCTYPE html>" + "y" * (3 * STEP)
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "create_artifact", {"title": "Big", "html": big_html}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Here it is."), *msg_end(stop_reason="end_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "build it"})
    frames = [json.loads(ln) for ln in lines if ln.startswith("{")]
    progress = [f["skill_progress"] for f in frames if "skill_progress" in f]
    assert progress and progress[0]["name"] == "create_artifact"
    assert progress[0]["chars"] >= STEP
    # The card and the answer still arrive; the turn ends normally.
    assert any("program_artifact" in f for f in frames)
    assert "Here it is." in "\n".join(lines) and lines[-1] == "[DONE]"


def test_small_tool_calls_emit_no_progress_frame(monkeypatch):
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "create_artifact", {"title": "Tiny", "html": "<p>hi</p>"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "build it"})
    assert not any('"skill_progress"' in ln for ln in lines)


def test_named_arguments_reads_only_whole_top_level_names():
    read = EV.named_arguments
    # The path arrived whole; the content still streams and holds a quoted
    # "title" that is not the call's own.
    partial = '{"path": "site/index.html", "content": "<p>\\"title\\": \\"x\\" and more'
    assert read(partial) == {"path": "site/index.html"}
    # A nested title belongs to a slide, not to the call.
    deck = '{"slides": [{"title": "One"}], "filename": "deck.pptx", "notes": "unfin'
    assert read(deck) == {"filename": "deck.pptx"}
    # Nothing whole yet, or the content came first: no name, no error.
    assert read('{"path": "site/ind') == {}
    assert read('{"content": "text that still') == {}
    assert read("") == {}


def test_progress_frame_names_the_file_when_the_path_came_first(monkeypatch):
    client = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block(
                    "t1", "ws_write_file", {"path": "site/index.html", "content": "z" * (3 * STEP)}
                ),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "build it"})
    frames = [json.loads(ln) for ln in lines if ln.startswith("{")]
    progress = [f["skill_progress"] for f in frames if "skill_progress" in f]
    assert progress and all(p["input"] == {"path": "site/index.html"} for p in progress)


def test_openai_adapter_names_the_file_in_its_progress(monkeypatch):
    encoded = json.dumps({"path": "notes/plan.md", "content": "x" * (3 * STEP)})
    chunks = [encoded[i : i + 3000] for i in range(0, len(encoded), 3000)]
    a, _fr = _adapter(monkeypatch, [_fc_round(chunks)])
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "go"}], [], None, 0, False))
    )
    progress = [e for e in evs if isinstance(e, EV.ToolCallProgress)]
    assert progress and all(p.named == {"path": "notes/plan.md"} for p in progress)


def test_a_quiet_tool_call_reports_its_name_before_the_content(monkeypatch):
    """The API holds a file's content back until it is whole. The adapter
    reports the path after a quiet moment, long before the content."""
    import server.chat.engine.anthropic as AN

    monkeypatch.setattr(AN, "TOOL_EARLY_REPORT_S", 0.05)
    given = {"path": "site/index.html", "content": "<p>short</p>"}
    start, delta, stop = tool_use_block("t1", "ws_write_file", given)
    encoded = delta["delta"]["partial_json"]
    cut = encoded.index('"content"')
    first = {**delta, "delta": {**delta["delta"], "partial_json": encoded[:cut]}}
    rest = {**delta, "delta": {**delta["delta"], "partial_json": encoded[cut:]}}
    client = FakeBedrockClient(
        [
            [msg_start(), start, first, PAUSE, rest, stop, *msg_end(stop_reason="tool_use")],
            [msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")],
        ]
    )
    lines = run_chat_turn(monkeypatch, client, {"question": "build it"})
    frames = [json.loads(ln) for ln in lines if ln.startswith("{")]
    progress = [f["skill_progress"] for f in frames if "skill_progress" in f]
    assert [p["input"] for p in progress] == [{"path": "site/index.html"}]
    assert progress[0]["chars"] < STEP
    # The call still runs with its whole input.
    (whole,) = [f for f in frames if f.get("skill_input") == "ws_write_file"]
    assert whole["input"] == given


def test_claude_tools_stream_their_arguments(monkeypatch):
    """Every tool of a Claude request asks for its arguments as they are
    written, and the shared tool pool is left as it was."""
    from server.chat.tool_pool import assemble_full_catalog

    before = json.dumps(assemble_full_catalog(), sort_keys=True)
    client = FakeBedrockClient(
        [[msg_start(), *text_block("Hello."), *msg_end(stop_reason="end_turn")]]
    )
    run_chat_turn(monkeypatch, client, {"question": "hi"})
    sent = client.requests[0]["tools"]
    assert sent and all(tool.get("eager_input_streaming") is True for tool in sent)
    assert json.dumps(assemble_full_catalog(), sort_keys=True) == before
    assert "eager_input_streaming" not in before
