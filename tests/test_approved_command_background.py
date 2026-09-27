"""An approved ws_run_command runs through the same path as a read-only one.

Before, anything that was not read-only (``npm run dev``, ``npm start``,
``python3 -m http.server``) became an approval payload carrying only the
command and cwd, then ran in the foreground under a hard 120 s timeout that
killed the whole process group and told the model "Command timed out".
``run_in_background`` and the 30 s handoff the tool description promises
never applied, and the session id (its cwd, its task ownership) was lost.

These tests drive the real round trip: the tool executor builds the approval
sentinel, the registered spec shapes the payload exactly as the approval card
and the inline auto-approve path receive it, and the approval executor runs it.
"""

import asyncio
import json
import os
import time

import pytest

from server.tasks import registry, shell


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("server.workspace.executors.get_workspace_path", lambda: str(root))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(root))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    from server.approval.bootstrap import register_defaults

    register_defaults()
    return root


def _approved_payload(command: str, session_id: str, **extra) -> dict:
    """What the approval executor receives after the user says yes."""
    from server.approval import registry as approval_registry
    from server.workspace.executors import _exec_ws_run_command

    out = _exec_ws_run_command({"command": command, "__session_id__": session_id, **extra}, "", [])
    assert out.startswith("[WS_APPROVAL]"), out
    parsed = json.loads(out[len("[WS_APPROVAL]") :])
    return approval_registry.get(parsed["action"]).build_payload(parsed)


def _run(payload: dict):
    from server.approval.executors import _do_command

    return asyncio.run(_do_command(payload))


def _wait_status(task_id: str, statuses: set[str], timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = registry.get_task(task_id)
        if task and task["status"] in statuses:
            return task
        time.sleep(0.05)
    return registry.get_task(task_id) or {}


def _task_id(text: str) -> str:
    assert "[Background Task Started] task_id=" in text, text
    return text.split("task_id=", 1)[1].split()[0]


def test_approved_run_in_background_starts_a_session_owned_task(ws):
    payload = _approved_payload("sleep 5 && echo late", "s-bg", run_in_background=True)
    started = time.monotonic()
    outcome = _run(payload)
    assert outcome.ok, outcome.error
    assert time.monotonic() - started < 3, "run_in_background must return at once"
    task_id = _task_id(outcome.output)
    task = registry.get_task(task_id)
    assert task["status"] == "running"
    assert task["session_id"] == "s-bg", "the approved task must belong to its session"
    assert shell.stop_task(task_id)
    assert _wait_status(task_id, {"stopped"})["status"] == "stopped"


def test_approved_long_command_is_handed_off_not_killed(ws, monkeypatch):
    """A foreground command still running at the budget keeps running as a
    background task and finishes its work; nothing is killed."""
    monkeypatch.setattr("server.workspace.executors._AUTO_BACKGROUND_SECONDS", 0.3)
    marker = ws / "finished.txt"
    payload = _approved_payload(f"sleep 1.2 && echo done > {marker}", "s-handoff")
    outcome = _run(payload)
    assert outcome.ok, outcome.error
    task_id = _task_id(outcome.output)
    assert registry.get_task(task_id)["session_id"] == "s-handoff"
    task = _wait_status(task_id, {"completed", "failed", "stopped"})
    assert task["status"] == "completed", task
    assert marker.read_text().strip() == "done"


def test_approved_command_runs_in_the_sessions_tracked_cwd(ws):
    from server.cwd_tracker import update_cwd

    sub = ws / "pkg"
    sub.mkdir()
    update_cwd("s-cwd", str(sub))
    outcome = _run(_approved_payload("touch here.txt", "s-cwd"))
    assert outcome.ok, outcome.error
    assert os.path.exists(sub / "here.txt"), "the approved command ignored the session cwd"


def test_approved_quick_command_still_returns_inline(ws):
    outcome = _run(_approved_payload("sh -c 'echo hello'; exit 3", "s-inline"))
    assert not outcome.ok
    assert "hello" in (outcome.output or "")
    assert outcome.error == "exit code 3"
    assert registry.list_tasks(session_id="s-inline") == []
