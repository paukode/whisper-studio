"""The skill_result frame's meta (server/tool_meta.py): the duration measured
around the dispatch, the size of what the model received and whether the
budget cut it, the lines a file read carries, and the diff stats of a write
the gate ran inline. Every key is always present, null when unknown, so the
client schema can declare them all. The call's id reaches a write on every
approval exit (the inline path, the card's POST, a spoken yes) so the edit is
kept for the diff dialog.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import file_state, tool_approvals
from server import tool_executor as te
from server.approval import registry
from server.approval.markers import approval_request, preview_image
from server.approval.router import router as approval_router
from server.approval.spec import ApprovalOutcome, ApprovalSpec
from server.chat.budget import TOOL_RESULT_BUDGET_BYTES, make_budget_tool_result
from server.tool_meta import META_KEYS, read_file_lines, result_meta
from tests.test_ask_user_pause_resume import _run, _State

NUMBERED = "    1\timport os\n    2\t\n    3\tprint(os.sep)"


def _dedup_stub() -> str:
    """What ws_read_file returns for an unchanged re-read (server/file_state.py)."""
    key = ("meta-test", "a.py")
    file_state.record_read(*key, __file__, None, None, 3)
    try:
        return file_state.check_dedup(*key, __file__, None, None)
    finally:
        file_state._read_state.pop(key, None)


STUB = _dedup_stub()


def _frames(sse_events, key):
    return [json.loads(e) for e in sse_events if f'"{key}"' in e]


def _meta(sse_events, tool_use_id):
    by_id = {f["tool_use_id"]: f for f in _frames(sse_events, "skill_result")}
    return by_id[tool_use_id]["meta"]


# ── Every key, always ───────────────────────────────────────────────────────


def test_every_key_is_present_null_when_unknown():
    _results, sse_events, _p, _q = _run([_State("tu_a", "ws_grep", "src/a.py:1: x")])
    meta = _meta(sse_events, "tu_a")
    assert tuple(meta) == META_KEYS
    assert meta == {
        "duration_ms": None,
        "output_chars": len("src/a.py:1: x"),
        "truncated": False,
        "lines": None,
        "bytes": None,
        "added": None,
        "removed": None,
    }


def test_the_preview_image_frame_carries_a_meta_too():
    payload = json.dumps({"caption": "Screen", "media_type": "image/jpeg", "data": "AAAA"})
    _results, sse_events, _p, _q = _run(
        [_State("tu_i", "preview_screenshot", preview_image(payload))]
    )
    (frame,) = _frames(sse_events, "skill_result")
    assert tuple(frame["meta"]) == META_KEYS
    assert frame["meta"]["output_chars"] == len("Screen")


# ── A file read counts its numbered lines ───────────────────────────────────


def test_read_file_lines_counts_numbered_lines_only():
    assert read_file_lines(NUMBERED) == 3
    # An empty file (or a range past its end) reads as no text at all.
    assert read_file_lines("") == 0
    big = NUMBERED + "\n\n... [40 lines omitted: over the token budget] ...\n\n"
    big += "   44\tend"
    assert read_file_lines(big) == 4
    assert read_file_lines(STUB) is None


def test_ws_read_file_meta_lines_is_the_count_or_null_for_the_stub_and_an_error():
    states = [
        _State("tu_r", "ws_read_file", NUMBERED),
        _State("tu_s", "ws_read_file", STUB),
        _State("tu_e", "ws_read_file", "Error reading: boom"),
        _State("tu_o", "ws_grep", NUMBERED),
    ]
    _results, sse_events, _p, _q = _run(states)
    assert _meta(sse_events, "tu_r")["lines"] == 3
    assert _meta(sse_events, "tu_s")["lines"] is None
    assert _meta(sse_events, "tu_e")["lines"] is None
    # Only a file read reports lines from its output.
    assert _meta(sse_events, "tu_o")["lines"] is None


def test_a_read_that_returned_no_file_text_has_no_line_count(tmp_path, monkeypatch):
    """The messages ws_read_file returns instead of file text are not error
    results, but they hold no lines: a '0 lines' chip would be wrong."""
    from server.workspace import executors as ws_exec

    root = tmp_path / "proj"
    root.mkdir()
    (root / "pic.png").write_bytes(b"\x89PNG")
    (root / "empty.txt").write_text("")
    monkeypatch.setattr(ws_exec, "get_workspace_path", lambda: str(root))
    read = ws_exec._exec_ws_read_file

    def _out(path):
        return read({"path": path}, "", None)

    outputs = {
        "tu_missing": _out("missing.py"),
        "tu_binary": _out("pic.png"),
        "tu_outside": _out("../../etc/hosts"),
        "tu_empty": _out("empty.txt"),
    }
    monkeypatch.setattr(ws_exec, "get_workspace_path", lambda: None)
    outputs["tu_nows"] = _out("a.py")
    assert outputs["tu_missing"].startswith("File not found")
    assert outputs["tu_binary"].startswith("Binary file")
    assert outputs["tu_nows"] == "No workspace connected."
    assert outputs["tu_empty"] == ""
    states = [_State(i, "ws_read_file", out) for i, out in outputs.items()]
    _results, sse_events, _p, _q = _run(states)
    frames = {f["tool_use_id"]: f for f in _frames(sse_events, "skill_result")}
    for tool_id in ("tu_missing", "tu_binary", "tu_outside", "tu_nows"):
        assert frames[tool_id]["meta"]["lines"] is None, tool_id
    # These three are not flagged as errors, so the line rule itself must
    # tell them from file text (the outside-roots refusal is an error).
    for tool_id in ("tu_missing", "tu_binary", "tu_nows"):
        assert frames[tool_id]["is_error"] is False, tool_id
    assert _meta(sse_events, "tu_empty")["lines"] == 0


# ── The budget ──────────────────────────────────────────────────────────────


def test_output_chars_and_truncated_describe_what_the_model_received(monkeypatch):
    from server.infrastructure import result_cache

    monkeypatch.setattr(result_cache, "write", lambda name, output: "cached.txt")
    monkeypatch.setattr(result_cache, "relative_path", lambda name: f"result_cache/{name}")
    big = "y" * (TOOL_RESULT_BUDGET_BYTES + 100)
    small = "fine"
    events: list[dict] = []
    results, sse_events, _p, _q = asyncio.run(
        te.process_tool_results(
            [_State("tu_big", "ws_grep", big), _State("tu_small", "ws_grep", small)],
            budget_fn=make_budget_tool_result(events),
            session_approvals={},
            config={},
            model_id="test-model",
            recent_messages=[],
        )
    )
    sent = {r["tool_use_id"]: r["content"] for r in results}
    big_meta = _meta(sse_events, "tu_big")
    assert big_meta["truncated"] is True
    assert big_meta["output_chars"] == len(sent["tu_big"]) < len(big)
    small_meta = _meta(sse_events, "tu_small")
    assert small_meta["truncated"] is False
    assert small_meta["output_chars"] == len(small)


# ── The duration is measured around the dispatch ────────────────────────────


def _batch(monkeypatch, route, tool_uses, **kw):
    monkeypatch.setattr(te, "route_tool", route)
    executor = ThreadPoolExecutor(max_workers=1)

    async def _go():
        return await te.execute_tool_batch(
            tool_uses,
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="s1",
            session_denials={},
            model_id="",
            **{"plan_mode": False, **kw},
        )

    try:
        return asyncio.run(_go())
    finally:
        executor.shutdown(wait=False)


def test_duration_is_measured_around_route_tool_and_kept_on_a_failure(monkeypatch):
    async def route(tool_name, call_input, **kw):
        await asyncio.sleep(0.02)
        if tool_name == "boom":
            raise RuntimeError("no")
        return ("ok", [])

    states = _batch(
        monkeypatch,
        route,
        [
            {"id": "t1", "name": "ws_grep", "input": {}},
            {"id": "t2", "name": "boom", "input": {}},
        ],
    )
    assert all(isinstance(s.duration_ms, int) and s.duration_ms >= 20 for s in states)
    _results, sse_events, _p, _q = _run(states)
    assert _meta(sse_events, "t1")["duration_ms"] == states[0].duration_ms
    assert _meta(sse_events, "t2")["duration_ms"] == states[1].duration_ms


def test_a_call_that_never_ran_has_no_duration(monkeypatch):
    async def route(tool_name, call_input, **kw):
        raise AssertionError("must not dispatch")

    (state,) = _batch(
        monkeypatch,
        route,
        [{"id": "t1", "name": "ws_write_file", "input": {"path": "a"}}],
        plan_mode=True,
    )
    assert state.status == "skipped"
    assert state.duration_ms is None
    _results, sse_events, _p, _q = _run([state])
    assert _meta(sse_events, "t1")["duration_ms"] is None


# ── A write the gate ran inline: the outcome's stats ride on the frame ──────


def _write_sentinel(action="test_meta_write"):
    payload = json.dumps({"action": action, "path": "a.py", "content": "x\n"})
    return approval_request(payload)


def _register(captured: list[dict], stats: dict | None, action="test_meta_write"):
    async def fake_exec(payload):
        captured.append(dict(payload))
        return ApprovalOutcome(ok=True, output="Wrote a.py (2 bytes).", stats=stats)

    registry.register(
        action,
        ApprovalSpec(category="test-meta", preview="diff", summary="w", executor=fake_exec),
    )


def test_an_inline_write_puts_its_stats_in_the_meta_and_keeps_its_id():
    captured: list[dict] = []
    stats = {"added": 3, "removed": 1, "lines": 12, "bytes": 240, "stored": True}
    _register(captured, stats)
    state = _State("toolu_w", "ws_write_file", _write_sentinel())
    results, sse_events, pending, _q = asyncio.run(
        te.process_tool_results(
            [state],
            budget_fn=lambda n, o: o,
            session_approvals={"test-meta": "allow"},
            config={},
            model_id="test-model",
            recent_messages=[],
        )
    )
    assert pending is False
    assert results[0]["content"] == "Wrote a.py (2 bytes)."
    meta = _meta(sse_events, "toolu_w")
    assert (meta["added"], meta["removed"], meta["bytes"]) == (3, 1, 240)
    # The written file's line count stays in the stats: the meta's lines is
    # the lines a read returned, one meaning only.
    assert meta["lines"] is None
    assert meta["truncated"] is False
    # The executor saw the call's id, stamped after build_payload.
    assert captured[0]["__tool_use_id__"] == "toolu_w"


def test_an_inline_run_counts_in_the_duration():
    """route_tool only returned the sentinel; the action ran in the gate (a
    session "Yes, all", auto mode, bypass, every unattended agent call), so
    its time is added to the call's duration."""

    async def slow_exec(payload):
        await asyncio.sleep(0.3)
        return ApprovalOutcome(ok=True, output="ran for a while")

    registry.register(
        "test_meta_slow",
        ApprovalSpec(category="test-meta", preview="text", summary="s", executor=slow_exec),
    )
    payload = json.dumps({"action": "test_meta_slow", "command": "x"})
    for unattended, approvals in ((False, {"test-meta": "allow"}), (True, {})):
        state = _State("toolu_slow", "ws_run_command", approval_request(payload))
        state.duration_ms = 5  # what route_tool took to return the sentinel
        _results, sse_events, _p, _q = asyncio.run(
            te.process_tool_results(
                [state],
                budget_fn=lambda n, o: o,
                session_approvals=approvals,
                config={},
                model_id="test-model",
                recent_messages=[],
                unattended=unattended,
            )
        )
        duration = _meta(sse_events, "toolu_slow")["duration_ms"]
        assert duration >= 305, (unattended, duration)
        assert duration < 5000


def test_an_unattended_inline_write_does_the_same():
    captured: list[dict] = []
    stats = {"added": 1, "removed": 0, "lines": 1, "bytes": 2, "stored": True}
    _register(captured, stats, action="test_meta_write_agent")
    state = _State("toolu_agent", "ws_write_file", _write_sentinel("test_meta_write_agent"))
    _results, sse_events, _p, _q = asyncio.run(
        te.process_tool_results(
            [state],
            budget_fn=lambda n, o: o,
            session_approvals={},
            config={},
            model_id="test-model",
            recent_messages=[],
            unattended=True,
        )
    )
    meta = _meta(sse_events, "toolu_agent")
    assert (meta["added"], meta["bytes"]) == (1, 2)
    assert captured[0]["__agent__"] is True
    assert captured[0]["__tool_use_id__"] == "toolu_agent"


def test_an_action_with_no_stats_leaves_the_write_keys_null():
    captured: list[dict] = []
    _register(captured, None, action="test_meta_nostats")
    state = _State("toolu_n", "ws_write_file", _write_sentinel("test_meta_nostats"))
    _results, sse_events, _p, _q = asyncio.run(
        te.process_tool_results(
            [state],
            budget_fn=lambda n, o: o,
            session_approvals={"test-meta": "allow"},
            config={},
            model_id="test-model",
            recent_messages=[],
        )
    )
    meta = _meta(sse_events, "toolu_n")
    assert (meta["added"], meta["removed"], meta["lines"], meta["bytes"]) == (None,) * 4


def test_inline_result_text_is_what_the_model_used_to_read():
    assert (
        tool_approvals.inline_result_text("write", ApprovalOutcome(ok=True, output="done"))
        == "done"
    )
    assert tool_approvals.inline_result_text("write", ApprovalOutcome(ok=True)) == "[OK] write"
    assert tool_approvals.inline_result_text(
        "command", ApprovalOutcome(ok=False, error="exit code 1")
    ) == ("Error: exit code 1")
    assert tool_approvals.inline_result_text(
        "command", ApprovalOutcome(ok=False, error="exit code 1", output="partial")
    ) == ("Error: exit code 1\n\npartial")
    assert (
        tool_approvals.inline_result_text("x", ApprovalOutcome(ok=False)) == "Error: unknown error"
    )


def test_a_crashing_or_unknown_action_is_a_failed_outcome():
    async def boom(payload):
        raise RuntimeError("disk full")

    registry.register(
        "test_meta_crash",
        ApprovalSpec(category="test-meta", preview="text", summary="c", executor=boom),
    )
    crashed = asyncio.run(tool_approvals._execute_ws_approval_inline({"action": "test_meta_crash"}))
    assert crashed.ok is False and "disk full" in crashed.error
    unknown = asyncio.run(
        tool_approvals._execute_ws_approval_inline({"action": "no_such_action_meta"})
    )
    assert unknown.ok is False and "no ApprovalSpec registered" in unknown.error


def test_result_meta_reads_a_states_stats_and_duration():
    state = _State("t", "ws_write_file", "Wrote a.py")
    state.duration_ms = 42
    state.stats = {"added": 2, "removed": 0, "lines": 5, "bytes": 50}
    assert result_meta(state, "Wrote a.py", "Wrote a.py", is_error=False) == {
        "duration_ms": 42,
        "output_chars": 10,
        "truncated": False,
        "lines": None,
        "bytes": 50,
        "added": 2,
        "removed": 0,
    }


# ── The card's POST and a spoken yes carry the id, and the POST the stats ──


def test_the_card_post_stamps_the_id_and_returns_the_stats():
    captured: list[dict] = []
    stats = {"added": 1, "removed": 1, "lines": 3, "bytes": 9, "stored": True}
    _register(captured, stats, action="test_meta_card")
    app = FastAPI()
    app.include_router(approval_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/approval/execute",
            json={
                "action": "test_meta_card",
                "payload": {"path": "a.py"},
                "tool_use_id": "toolu_c",
            },
        )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert res.json()["stats"] == stats
    assert captured[0]["__tool_use_id__"] == "toolu_c"
    assert captured[0]["path"] == "a.py"


def test_the_card_post_without_an_id_stamps_nothing():
    captured: list[dict] = []
    _register(captured, None, action="test_meta_card_noid")
    app = FastAPI()
    app.include_router(approval_router)
    with TestClient(app) as client:
        res = client.post(
            "/api/approval/execute", json={"action": "test_meta_card_noid", "payload": {"p": 1}}
        )
    assert res.json()["stats"] is None
    assert "__tool_use_id__" not in captured[0]


def test_a_spoken_yes_passes_the_requests_id_to_the_executor():
    from server.voice.tools import _execute_approval

    captured: list[dict] = []
    _register(captured, None, action="test_meta_voice")
    ok, detail = asyncio.run(
        _execute_approval("test_meta_voice", {"path": "a.py"}, tool_use_id="toolu_v")
    )
    assert ok is True and detail == "Wrote a.py (2 bytes)."
    assert captured[0]["__tool_use_id__"] == "toolu_v"
    asyncio.run(_execute_approval("test_meta_voice", {"path": "a.py"}))
    assert "__tool_use_id__" not in captured[1]
