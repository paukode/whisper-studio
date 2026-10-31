"""A workflow never runs on a guessed node path.

With no WHISPER_NODE_PATH and no node on PATH the runtime used to fall back
to /usr/local/bin/node, so the run died later on a spawn error for a path
nobody chose. Now it refuses up front, and the callers report a missing node
as its own error instead of calling it a script error.
"""

import asyncio

import pytest

from server.workflows import runtime
from server.workflows import tools as wf_tools

_SCRIPT = "export const meta = {name: 'x', description: 'y', phases: []};"


@pytest.fixture
def no_node(monkeypatch):
    monkeypatch.setattr(runtime, "resolve", lambda *a, **k: None)


def test_the_harness_refuses_when_no_node_is_found(no_node):
    with pytest.raises(runtime.NodeNotFoundError, match="WHISPER_NODE_PATH"):
        runtime._node_bin()


def test_a_found_node_is_used_as_is(monkeypatch):
    monkeypatch.setattr(runtime, "resolve", lambda *a, **k: "/opt/homebrew/bin/node")
    assert runtime._node_bin() == "/opt/homebrew/bin/node"


def test_parse_reports_a_missing_node_not_a_script_problem(no_node):
    with pytest.raises(runtime.NodeNotFoundError):
        runtime.parse_workflow(_SCRIPT)


def test_workflow_save_says_node_is_missing(no_node):
    out = asyncio.run(wf_tools.execute_workflow_save({"name": "x", "script": _SCRIPT}))
    assert out.startswith("Error: Node.js was not found")
    assert "script error" not in out


def test_a_live_run_fails_with_the_reason(no_node):
    run = runtime.WorkflowRun("wf_node_missing", _SCRIPT, journal=object())
    result = asyncio.run(run.run())
    assert result["status"] == "failed"
    assert "Node.js was not found" in result["error"]
