"""Programmatic tool calling (run_tool_script): a model-written script
orchestrates READ-ONLY tools in one turn, intermediate results stay out of
context, and a non-read-only tool called from inside is refused.

The read-only gate is tested directly (no Node needed); the end-to-end script
execution is Node-gated and skipped where node is unavailable.
"""

import asyncio
import importlib
import os
import shutil
import tempfile

import pytest

# Make sure the read/search executors are registered so is_read_only() knows
# them (main.py does this at boot; tests import them explicitly).
for _m in (
    "server.executors.tool_script",
    "server.search",
    "server.workspace.executors",
    "server.git.executor",
):
    try:
        importlib.import_module(_m)
    except Exception:
        pass

from server.executors import is_read_only  # noqa: E402
from server.workflows.runtime import WorkflowRun  # noqa: E402

_HAS_NODE = shutil.which("node") is not None or bool(os.environ.get("WHISPER_NODE_PATH"))
_node_only = pytest.mark.skipif(not _HAS_NODE, reason="needs node for the harness")


def test_read_only_flags_are_correct():
    assert is_read_only("ws_read_file")
    assert is_read_only("ws_glob")
    assert is_read_only("ws_grep")
    assert is_read_only("git_status")
    assert not is_read_only("ws_write_file")
    assert not is_read_only("terminal_run")
    assert not is_read_only("run_python")
    # Fail-closed for unknowns.
    assert not is_read_only("nonexistent_tool")


def _drive_handle_tool(name, args):
    """Call WorkflowRun._handle_tool directly, capturing the RPC reply."""
    run = WorkflowRun("toolscript-test", "", session_id="s1")
    captured = {}

    async def fake_respond(mid, result):
        captured["ok"] = result

    async def fake_error(mid, err_type, message):
        captured["error"] = (err_type, message)

    run._respond = fake_respond
    run._error = fake_error
    asyncio.run(run._handle_tool(1, {"name": name, "args": args}))
    return captured


def test_gate_refuses_a_write_tool():
    cap = _drive_handle_tool("ws_write_file", {"path": "x", "content": "y"})
    assert "ok" not in cap
    assert "error" in cap
    assert "not read-only" in cap["error"][1]


def test_gate_refuses_an_unknown_tool():
    cap = _drive_handle_tool("totally_made_up", {})
    assert "error" in cap and "not read-only" in cap["error"][1]


def test_gate_rejects_non_object_args():
    run = WorkflowRun("toolscript-test2", "", session_id="s1")
    captured = {}

    async def fake_error(mid, err_type, message):
        captured["error"] = message

    run._error = fake_error
    asyncio.run(run._handle_tool(1, {"name": "ws_read_file", "args": ["not", "a", "dict"]}))
    assert "must be an object" in captured["error"]


def test_gate_allows_a_read_tool_and_returns_output(monkeypatch):
    import server.skills as skills

    monkeypatch.setattr(skills, "execute_tool", lambda name, args: f"CONTENT of {args.get('path')}")
    cap = _drive_handle_tool("ws_read_file", {"path": "a.txt"})
    assert "error" not in cap
    assert cap["ok"] == {"output": "CONTENT of a.txt"}


def test_bulky_result_is_spilled_in_the_journal(monkeypatch, tmp_path):
    import server.skills as skills
    from server.workflows import journal as journal_mod

    big = "x" * 50_000
    monkeypatch.setattr(skills, "execute_tool", lambda name, args: big)

    run = WorkflowRun("toolscript-spill", "", session_id="s1")
    writes = []
    run.journal.tool_call = lambda entry: writes.append(entry)
    run._respond = _async_noop
    run._error = _async_noop

    asyncio.run(run._handle_tool(1, {"name": "ws_read_file", "args": {"path": "big"}}))
    assert writes, "a tool_call journal entry must be written"
    entry = writes[-1]
    assert entry["bytes"] == 50_000
    # The DURABLE copy is a preview, not the whole 50k.
    assert len(entry["preview"]) <= WorkflowRun._TOOL_LOG_PREVIEW


async def _async_noop(*a, **k):
    return None


# ── Node-gated end to end ────────────────────────────────────────────────────


@_node_only
def test_script_orchestrates_reads_in_one_turn():
    from server.executors.tool_script import do_run_tool_script
    from server.workspace.state import set_workspace_override

    d = tempfile.mkdtemp(prefix="ptc-e2e-")
    try:
        open(os.path.join(d, "a.txt"), "w").write("alpha beta")
        open(os.path.join(d, "b.txt"), "w").write("one two three")
        set_workspace_override(d)
        src = (
            'const a = await tools.ws_read_file({path:"a.txt"});\n'
            'const b = await tools.ws_read_file({path:"b.txt"});\n'
            "return JSON.stringify({a_len: a.length > 0, b_len: b.length > 0, "
            'has_alpha: a.includes("alpha"), has_two: b.includes("two")});'
        )
        ok, text = asyncio.run(do_run_tool_script({"code": src, "session_id": "s1"}))
        assert ok, text
        assert '"has_alpha":true' in text.replace(" ", "")
        assert '"has_two":true' in text.replace(" ", "")
    finally:
        set_workspace_override(None)
        shutil.rmtree(d, ignore_errors=True)


@_node_only
def test_script_write_tool_is_refused_end_to_end():
    from server.executors.tool_script import do_run_tool_script

    ok, text = asyncio.run(
        do_run_tool_script(
            {
                "code": 'return await tools.ws_write_file({path:"x",content:"y"});',
                "session_id": "s1",
            }
        )
    )
    assert ok is False
    assert "not read-only" in text


@_node_only
def test_agent_is_unavailable_in_a_tool_script():
    from server.executors.tool_script import do_run_tool_script

    ok, text = asyncio.run(
        do_run_tool_script({"code": 'return await agent("hi");', "session_id": "s1"})
    )
    assert ok is False
    assert "unavailable" in text or "agent" in text.lower()


@_node_only
def test_empty_code_is_refused():
    from server.executors.tool_script import do_run_tool_script

    ok, text = asyncio.run(do_run_tool_script({"code": "   ", "session_id": "s1"}))
    assert ok is False and "code is required" in text
