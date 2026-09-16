"""server.chat.loop_guard: per-turn refusal of identical repeats, repeated
failures, call cycles and per-turn caps, plus its wiring through
execute_tool_batch's guard_scope."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import tool_executor as te
from server.chat import loop_guard as lg

SCOPE = "turn-1"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    lg.reset_for_turn(SCOPE)
    monkeypatch.setattr(lg, "enabled", lambda: True)
    monkeypatch.setattr(lg, "caps", lambda: dict(lg.DEFAULT_CAPS))
    yield
    lg.reset_for_turn(SCOPE)


def _cycle(name, args, result, is_error=False):
    d = lg.before_call(SCOPE, name, args)
    if d.allow:
        lg.after_call(SCOPE, name, args, result, is_error=is_error)
    return d


def test_third_identical_call_with_identical_results_is_refused():
    assert _cycle("ws_grep", {"pattern": "x"}, "same").allow
    assert _cycle("ws_grep", {"pattern": "x"}, "same").allow
    d = lg.before_call(SCOPE, "ws_grep", {"pattern": "x"})
    assert d.allow is False and d.reason == "identical_streak"
    assert "same result 2 times" in d.message


def test_second_identical_result_gets_a_notice_and_large_duplicates_a_stub():
    args = {"path": "a.py"}
    big = "x" * (lg.STUB_MIN_CHARS + 10)
    lg.before_call(SCOPE, "ws_read_file", args)
    notice, stub = lg.after_call(SCOPE, "ws_read_file", args, big, is_error=False)
    assert notice is None and stub is None
    lg.before_call(SCOPE, "ws_read_file", args)
    notice, stub = lg.after_call(SCOPE, "ws_read_file", args, big, is_error=False)
    assert notice and "number 2" in notice
    assert stub and "Identical to the previous ws_read_file result" in stub


def test_a_different_result_resets_the_streak():
    assert _cycle("ws_grep", {"pattern": "x"}, "one").allow
    assert _cycle("ws_grep", {"pattern": "x"}, "two").allow
    assert _cycle("ws_grep", {"pattern": "x"}, "three").allow
    assert lg.before_call(SCOPE, "ws_grep", {"pattern": "x"}).allow


def test_repeated_identical_failure_is_blocked_after_three():
    for _ in range(lg.FAILURE_STREAK_LIMIT):
        assert _cycle("ws_run_command", {"command": "boom"}, "[Tool Error] no", True).allow
    d = lg.before_call(SCOPE, "ws_run_command", {"command": "boom"})
    assert d.allow is False and d.reason == "repeated_failure"


def test_success_clears_the_failure_count():
    _cycle("ws_run_command", {"command": "c"}, "[Tool Error] no", True)
    _cycle("ws_run_command", {"command": "c"}, "[Tool Error] no", True)
    _cycle("ws_run_command", {"command": "c"}, "ok now", False)
    _cycle("ws_run_command", {"command": "c"}, "[Tool Error] no", True)
    assert lg.before_call(SCOPE, "ws_run_command", {"command": "c"}).allow


def test_repeating_cycle_of_two_calls_is_refused():
    for _ in range(lg.CYCLE_REPEATS):
        assert _cycle("ws_read_file", {"path": "a"}, "A").allow
        assert _cycle("ws_grep", {"pattern": "b"}, "B").allow
    d = lg.before_call(SCOPE, "ws_read_file", {"path": "a"})
    assert d.allow is False and d.reason == "cycle"


def test_pollers_are_exempt_from_identical_streaks():
    for _ in range(6):
        assert _cycle("task_status", {"task_id": "t"}, "running").allow


def test_per_turn_cap_blocks_the_next_call(monkeypatch):
    monkeypatch.setattr(lg, "caps", lambda: {"web_search": 2})
    assert _cycle("web_search", {"q": "a"}, "r1").allow
    assert _cycle("web_search", {"q": "b"}, "r2").allow
    d = lg.before_call(SCOPE, "web_search", {"q": "c"})
    assert d.allow is False and d.reason == "cap"


def test_private_stamps_do_not_change_the_argument_identity():
    a = lg.canonical_args({"path": "x", "__session_id__": "s1", "__agent__": True})
    b = lg.canonical_args({"path": "x"})
    assert a == b


def test_empty_scope_or_disabled_flag_never_refuses(monkeypatch):
    assert lg.before_call("", "ws_grep", {}).allow
    monkeypatch.setattr(lg, "enabled", lambda: False)
    for _ in range(5):
        assert _cycle("ws_grep", {"pattern": "x"}, "same").allow


def _run_batch(monkeypatch, name, tool_input, output="same", guard_scope=SCOPE):
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
            guard_scope=guard_scope,
        )

    try:
        return asyncio.run(_go())[0]
    finally:
        executor.shutdown(wait=False)


def test_executor_refuses_the_third_identical_call_via_guard_scope(monkeypatch):
    first = _run_batch(monkeypatch, "ws_grep", {"pattern": "x"})
    second = _run_batch(monkeypatch, "ws_grep", {"pattern": "x"})
    third = _run_batch(monkeypatch, "ws_grep", {"pattern": "x"})
    assert first.status == "completed" and "Loop guard" not in first.output
    assert second.status == "completed" and "Loop guard note" in second.output
    assert third.status == "skipped" and third.output.startswith("[Loop guard]")


def test_executor_without_guard_scope_is_unchanged(monkeypatch):
    for _ in range(4):
        st = _run_batch(monkeypatch, "ws_grep", {"pattern": "x"}, guard_scope="")
        assert st.status == "completed" and st.output == "same"
