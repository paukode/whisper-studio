"""Work a command leaves running in its process group stays in reach of Stop.

A shell that backgrounds work and returns (``npm run dev &``, ``nohup ... &``)
used to finish inline with no registry row: nothing tracked the group that was
still running, so the turn-scoped Stop, the task panel and the runtime cap
never saw the dev server holding its port. The same gap hit a handed-off task
whose leader exited while a child kept running: the row was marked completed
at the leader's exit and ``stop_task`` refused a finished leader.
"""

import asyncio
import json
import os
import signal
import subprocess
import time

import pytest

from server.tasks import handoff, registry, shell


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")


def _wait(predicate, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _status(task_id: str) -> str:
    return (registry.get_task(task_id) or {}).get("status", "")


def _read_pid(path) -> int:
    assert _wait(lambda: path.exists() and path.read_text().strip()), "no child pid written"
    return int(path.read_text().strip())


def _cleanup(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def test_backgrounded_work_of_an_inline_command_is_tracked_and_stopped(tmp_path):
    pid_file = tmp_path / "bg.pid"
    cmd = f"(exec sleep 30) & echo $! > {pid_file}; echo started"
    since = time.time()
    result = handoff.run_with_handoff(cmd, cmd, cwd=str(tmp_path), session_id="s-linger")
    child = _read_pid(pid_file)
    try:
        assert result.background is False, "the command itself finished inline"
        assert result.returncode == 0
        assert "started" in result.output
        assert result.task_id, "the group it left running got no registry row"
        task = registry.get_task(result.task_id)
        assert task["status"] == "running"
        assert task["session_id"] == "s-linger"
        assert task["meta"].get("left_running") is True

        stopped = handoff.stop_turn_work("s-linger", since)
        assert result.task_id in stopped["stopped"]
        assert _wait(lambda: not _alive(child)), "Stop left the backgrounded child running"
        assert _wait(lambda: _status(result.task_id) == "stopped")
    finally:
        _cleanup(child)


def test_left_running_work_that_ends_on_its_own_completes_the_row(tmp_path):
    cmd = "(sleep 0.8) & echo started"
    result = handoff.run_with_handoff(cmd, cmd, cwd=str(tmp_path), session_id="s-short")
    assert result.task_id
    assert _wait(lambda: _status(result.task_id) == "completed")


def test_a_plain_command_leaves_no_row(tmp_path):
    result = handoff.run_with_handoff(
        "echo quick", "echo quick", cwd=str(tmp_path), session_id="s-plain"
    )
    assert result.task_id is None
    assert registry.list_tasks(session_id="s-plain") == []


def test_handed_off_task_whose_leader_exited_is_still_stopped(tmp_path):
    """Leader handed off at the budget, then exits while its child runs on:
    the row stays running and Stop still kills the child."""
    pid_file = tmp_path / "bg2.pid"
    cmd = f"(exec sleep 30) & echo $! > {pid_file}; sleep 1"
    since = time.time()
    result = handoff.run_with_handoff(
        cmd, cmd, cwd=str(tmp_path), session_id="s-leader", timeout=0.3
    )
    assert result.background is True
    child = _read_pid(pid_file)
    try:
        time.sleep(1.5)  # the leader's `sleep 1` is over; the child is not
        assert _alive(child)
        assert _status(result.task_id) == "running", "the row finished with its leader"
        stopped = handoff.stop_turn_work("s-leader", since)
        assert result.task_id in stopped["stopped"]
        assert _wait(lambda: not _alive(child)), "Stop spared the child of an exited leader"
        assert _wait(lambda: _status(result.task_id) == "stopped")
    finally:
        _cleanup(child)


def test_adopted_fallback_reaches_the_group_after_its_leader_exited(tmp_path):
    """A task with no live handle (after a restart) is stopped by its row's
    pid, which is the group id even once the leader itself is gone."""
    from server.process_utils import new_process_group

    pid_file = tmp_path / "bg3.pid"
    leader = subprocess.Popen(
        ["/bin/sh", "-c", f"(exec sleep 30) & echo $! > {pid_file}"],
        preexec_fn=new_process_group,
    )
    leader.wait(timeout=5)
    child = _read_pid(pid_file)
    task_id = registry.create_task("shell", session_id="s-adopt", title="dev", command="dev")
    registry.attach_pid(task_id, leader.pid)
    try:
        assert shell.stop_task(task_id)
        assert _wait(lambda: not _alive(child))
    finally:
        _cleanup(child)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("server.workspace.executors.get_workspace_path", lambda: str(root))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(root))
    from server.approval.bootstrap import register_defaults

    register_defaults()
    return root


def test_approved_backgrounding_command_is_stopped_by_its_turn(ws):
    """The real approved path: the tool builds the approval payload, the
    approval executor runs it, and the turn's Stop kills what it left running."""
    from server.approval import registry as approval_registry
    from server.approval.executors import _do_command
    from server.workspace.executors import _exec_ws_run_command

    pid_file = ws / "dev.pid"
    command = f"(exec sleep 30) & echo $! > {pid_file}; echo dev-started"
    since = time.time()
    out = _exec_ws_run_command({"command": command, "__session_id__": "s-appr"}, "", [])
    assert out.startswith("[WS_APPROVAL]"), out
    parsed = json.loads(out[len("[WS_APPROVAL]") :])
    payload = approval_registry.get(parsed["action"]).build_payload(parsed)

    outcome = asyncio.run(_do_command(payload))
    child = _read_pid(pid_file)
    try:
        assert outcome.ok, outcome.error
        assert "dev-started" in outcome.output
        assert "[Left running]" in outcome.output
        handoff.stop_turn_work("s-appr", since)
        assert _wait(lambda: not _alive(child)), "Stop spared the approved command's server"
    finally:
        _cleanup(child)
