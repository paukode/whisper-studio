"""Tool-argument streaming progress: a whole HTML app arrives as one tool
call's arguments and can take minutes; both adapters now report the size
received every TOOL_ARGS_PROGRESS_STEP chars and the engine forwards it as a
``skill_progress`` frame. Small calls stay frame-free (golden fixtures pin
the frame vocabulary for those)."""

from __future__ import annotations

import asyncio
import json

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
    piece = '{"html": "' + "x" * 2990 + '"'  # 3000 chars each
    a, _fr = _adapter(monkeypatch, [_fc_round([piece, ",", piece[1:], piece[1:] + "}"])])
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "go"}], [], None, 0, False))
    )
    progress = [e for e in evs if isinstance(e, EV.ToolCallProgress)]
    assert progress, "expected progress events for a large tool call"
    assert all(p.name == "create_artifact" for p in progress)
    chars = [p.chars for p in progress]
    assert chars == sorted(chars) and chars[0] >= STEP
    # One report per crossed step, never per delta.
    total = sum(len(d) for d in [piece, ",", piece[1:], piece[1:] + "}"])
    assert len(progress) <= total // STEP + 1
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
