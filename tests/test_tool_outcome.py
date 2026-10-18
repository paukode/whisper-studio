"""The tool frames carry the provider's tool_use id, and the gate's one rule
for a failed result (tool_executor.tool_outcome) feeds both the loop guard's
repeated-failure count and the skill_result frame's is_error and exit_code.

The adapters only forward the id they were given; whether a result is an
error is decided in the gate, on the full output, never in an adapter."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import tool_executor as te
from server.chat import loop_guard as lg
from server.chat.engine import events as EV
from server.tool_executor import ToolOutcome, tool_outcome
from server.tool_router import route_tool
from tests.golden_harness import (
    FakeBedrockClient,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
    tool_use_block,
)
from tests.test_ask_user_pause_resume import _run, _State

# ── The rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prefix",
    [
        "[Tool Error]",
        "Error:",
        "Error ",
        "error:",
        "[Denied",
        "[Skipped]",
        "[Refused]",
        "[Plan Mode]",
        "[Hook denied]",
        "[Loop guard]",
    ],
)
def test_every_error_marker_reads_as_an_error(prefix):
    assert tool_outcome("ws_read_file", f"{prefix} something went wrong") == ToolOutcome(True, None)


def test_marker_must_open_the_output():
    assert tool_outcome("ws_read_file", "fine, then Error: later") == ToolOutcome(False, None)


def test_plain_result_of_a_non_command_tool_has_no_exit_code():
    assert tool_outcome("ws_read_file", "print(1)") == ToolOutcome(False, None)


def test_ws_run_command_without_a_suffix_exited_zero():
    assert tool_outcome("ws_run_command", "ok\nDone.") == ToolOutcome(False, 0)


def test_other_command_tools_without_a_suffix_report_no_exit_code():
    assert tool_outcome("run_python", "hello") == ToolOutcome(False, None)
    assert tool_outcome("terminal_run", "exit_code: 0\n---\nhello") == ToolOutcome(False, None)


def test_non_zero_exit_code_is_an_error_without_any_marker():
    assert tool_outcome("ws_run_command", "boom\n(exit code 3)") == ToolOutcome(True, 3)
    assert tool_outcome("run_python", "(exit code 1)\nTraceback") == ToolOutcome(True, 1)


def test_exit_code_with_a_meaning_and_a_negative_code_parse():
    assert tool_outcome("ws_run_command", "x\n(exit code 1: no matches)") == ToolOutcome(True, 1)
    assert tool_outcome("ws_run_command", "x\n(exit code -9)") == ToolOutcome(True, -9)


def test_exit_code_zero_in_the_suffix_is_not_an_error():
    assert tool_outcome("ws_run_command", "x\n(exit code 0: ok)") == ToolOutcome(False, 0)


def test_the_last_suffix_wins():
    out = "the script prints (exit code 7) itself\n(exit code 2)"
    assert tool_outcome("ws_run_command", out) == ToolOutcome(True, 2)


def test_a_refused_command_carries_no_exit_code():
    # The command never ran, so the silent-success default does not apply.
    assert tool_outcome("ws_run_command", "[Denied] 'ws_run_command' has been denied") == (
        ToolOutcome(True, None)
    )


def test_non_string_output_is_read_as_text():
    assert tool_outcome("ws_read_file", {"error": 1}) == ToolOutcome(False, None)


# ── The loop guard uses the same rule ───────────────────────────────────────


def _run_batch(monkeypatch, scope, name, tool_input, output):
    async def route(tool_name, call_input, **kw):
        return (output, [])

    monkeypatch.setattr(te, "route_tool", route)
    executor = ThreadPoolExecutor(max_workers=1)

    async def _go():
        return await te.execute_tool_batch(
            [{"id": "t", "name": name, "input": tool_input}],
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="s1",
            session_denials={},
            model_id="",
            plan_mode=False,
            guard_scope=scope,
        )

    try:
        return asyncio.run(_go())[0]
    finally:
        executor.shutdown(wait=False)


def test_loop_guard_counts_a_failure_the_gate_rule_names(monkeypatch):
    scope = "outcome-turn"
    lg.reset_for_turn(scope)
    monkeypatch.setattr(lg, "enabled", lambda: True)
    monkeypatch.setattr(lg, "caps", lambda: dict(lg.DEFAULT_CAPS))
    try:
        # A lowercase "error:" line and a non-zero exit code: neither opens
        # with the old four-marker tuple, both are failures under the one rule.
        _run_batch(monkeypatch, scope, "ws_grep", {"pattern": "x"}, "error: no index")
        _run_batch(monkeypatch, scope, "ws_run_command", {"command": "c"}, "x\n(exit code 2)")
        _run_batch(monkeypatch, scope, "ws_read_file", {"path": "p"}, "fine")
        failures = lg._state(scope).failures
        assert failures[("ws_grep", lg.canonical_args({"pattern": "x"}))] == 1
        assert failures[("ws_run_command", lg.canonical_args({"command": "c"}))] == 1
        assert ("ws_read_file", lg.canonical_args({"path": "p"})) not in failures
    finally:
        lg.reset_for_turn(scope)


# ── The skill_result frame carries the id and the outcome ───────────────────


def _frames(sse_events, key):
    return [json.loads(e) for e in sse_events if f'"{key}"' in e]


def test_skill_result_frame_carries_id_and_outcome():
    states = [
        _State("tu_a", "ws_run_command", "boom\n(exit code 3)"),
        _State("tu_b", "ws_read_file", "print(1)"),
    ]
    _results, sse_events, _pending, _question = _run(states)
    by_id = {f["tool_use_id"]: f for f in _frames(sse_events, "skill_result")}
    assert by_id["tu_a"] == {
        "skill_result": "ws_run_command",
        "output": "boom\n(exit code 3)",
        "tool_use_id": "tu_a",
        "is_error": True,
        "exit_code": 3,
    }
    assert by_id["tu_b"] == {
        "skill_result": "ws_read_file",
        "output": "print(1)",
        "tool_use_id": "tu_b",
        "is_error": False,
        "exit_code": None,
    }


def test_outcome_is_read_from_the_full_output_not_the_preview():
    long_tail = "x" * 3000 + "\n(exit code 4)"
    _results, sse_events, _pending, _question = _run([_State("tu_a", "ws_run_command", long_tail)])
    (frame,) = _frames(sse_events, "skill_result")
    assert frame["output"].endswith("...") and "(exit code" not in frame["output"]
    assert frame["is_error"] is True and frame["exit_code"] == 4


def test_sibling_emitted_before_a_pause_carries_id_and_outcome():
    # A workspace prompt pauses the batch; the sibling that already ran is
    # still reported, with the same frame shape as the normal path.
    prompt = _State("tu_p", "ws_write_file", '[WS_WORKSPACE_PROMPT]{"path": "a.txt"}')
    sibling = _State("tu_s", "ws_run_command", "built\n(exit code 1)")
    sibling.status = "completed"
    _results, sse_events, pending, _question = _run([prompt, sibling])
    assert pending is True
    (frame,) = _frames(sse_events, "skill_result")
    assert frame["tool_use_id"] == "tu_s"
    assert frame["is_error"] is True and frame["exit_code"] == 1


def test_sleep_side_effect_frames_carry_the_id():
    executor = ThreadPoolExecutor(max_workers=1)

    async def _go():
        return await route_tool(
            "sleep",
            {"seconds": 0},
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="s1",
            model_id="",
            tool_use_id="tu_sleep",
        )

    try:
        _output, side_effects = asyncio.run(_go())
    finally:
        executor.shutdown(wait=False)
    assert side_effects[0] == {
        "skill": "sleep",
        "input": {"seconds": 0.0},
        "tool_use_id": "tu_sleep",
    }
    assert side_effects[1] == {
        "skill_result": "sleep",
        "output": "Slept for 0.0 seconds.",
        "tool_use_id": "tu_sleep",
        "is_error": False,
        "exit_code": None,
    }


# ── The adapters forward the provider's id ──────────────────────────────────


def test_anthropic_frames_carry_the_block_id(monkeypatch):
    big_html = "<!DOCTYPE html>" + "y" * (3 * EV.TOOL_ARGS_PROGRESS_STEP)
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
    (start,) = [f for f in frames if "skill" in f]
    assert start == {"skill": "create_artifact", "input": {}, "tool_use_id": "t1"}
    progress = [f["skill_progress"] for f in frames if "skill_progress" in f]
    assert progress and all(p["tool_use_id"] == "t1" for p in progress)
    (call,) = [f for f in frames if "skill_input" in f]
    assert call["tool_use_id"] == "t1"
    (result,) = [f for f in frames if "skill_result" in f]
    assert result["tool_use_id"] == "t1"
    assert result["is_error"] is False and result["exit_code"] is None


def test_openai_events_carry_the_call_id(monkeypatch):
    from tests.test_engine_openai import _adapter, _collect
    from tests.test_tool_progress import _fc_round

    piece = '{"html": "' + "x" * (EV.TOOL_ARGS_PROGRESS_STEP + 10) + '"}'
    a, _fr = _adapter(monkeypatch, [_fc_round([piece])])
    evs = asyncio.run(
        _collect(a.stream_round([{"role": "user", "content": "go"}], [], None, 0, False))
    )
    (start,) = [e for e in evs if isinstance(e, EV.ToolCallStart)]
    assert start == EV.ToolCallStart(name="create_artifact", id="call_1")
    progress = [e for e in evs if isinstance(e, EV.ToolCallProgress)]
    assert progress and all(p.id == "call_1" for p in progress)
    (call,) = [e for e in evs if isinstance(e, EV.ToolCall)]
    assert call.id == "call_1"


def test_local_frames_carry_the_call_id(monkeypatch):
    from tests.test_engine_local import _run_local_turn

    calls = [{"id": "c1", "name": "ws_read_file", "input": {"path": "a.py"}}]
    blob, _fake = _run_local_turn(
        monkeypatch,
        [([], calls), ([("text", "done")], [])],
        tools_enabled=True,
        results_by_id={"c1": "print(1)"},
    )
    frames = [json.loads(ln[6:]) for ln in blob.splitlines() if ln.startswith("data: {")]
    (start,) = [f for f in frames if "skill" in f]
    assert start == {"skill": "ws_read_file", "input": {}, "tool_use_id": "c1"}
    (call,) = [f for f in frames if "skill_input" in f]
    assert call["tool_use_id"] == "c1"
