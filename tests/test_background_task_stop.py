"""Contract for the ESC kill switch's background-task half.

killSessionStream (frontend) fires POST /api/workspace/shell/tasks/stop with
its session_id; handoff.stop_turn_work must kill every RUNNING task that
session started and leave other sessions' tasks alone. Since the unified registry,
an ESC-killed task records the honest status 'stopped' (previously the
in-memory registry marked it 'completed').
"""

import time

import pytest

from server.tasks import handoff, registry, shell


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    yield


def _wait_status(task_id: str, status: str, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = registry.get_task(task_id)
        if task and task["status"] == status:
            return True
        time.sleep(0.05)
    return False


def test_stop_turn_work_kills_only_that_sessions_running_tasks(tmp_path):
    mine = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="sess-a")
    other = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="sess-b")
    time.sleep(0.3)

    stopped = handoff.stop_turn_work("sess-a", 0)["stopped"]

    assert mine["task_id"] in stopped
    assert other["task_id"] not in stopped
    # The waiter observes the kill and records the honest terminal status.
    assert _wait_status(mine["task_id"], "stopped")
    assert registry.get_task(other["task_id"])["status"] == "running"
    shell.stop_task(other["task_id"])


def test_stop_turn_work_handles_an_unknown_session(tmp_path):
    assert handoff.stop_turn_work("no-such-session", 0)["stopped"] == []


def test_natural_exit_records_exit_code_and_output_tail(tmp_path):
    info = shell.start_shell_task(
        "echo hello-from-task && exit 3", cwd=str(tmp_path), session_id="sess-c"
    )
    assert _wait_status(info["task_id"], "failed")
    task = registry.get_task(info["task_id"])
    assert task["exit_code"] == 3
    assert "hello-from-task" in task["result_text"]


def test_clean_exit_marks_completed(tmp_path):
    info = shell.start_shell_task("echo ok", cwd=str(tmp_path), session_id="sess-d")
    assert _wait_status(info["task_id"], "completed")
    assert registry.get_task(info["task_id"])["exit_code"] == 0


def test_runtime_cap_kills_a_runaway_command_and_records_why(tmp_path, monkeypatch):
    """Nothing bounded a background command before: a model-issued find over a
    whole home directory sat 'running' for hours. Past the cap the group is
    killed, the row records 'stopped', and the tail says why."""
    monkeypatch.setattr(shell, "MAX_RUNTIME_S", 1)
    info = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="sess-cap")

    assert _wait_status(info["task_id"], "stopped", timeout=8.0)
    task = registry.get_task(info["task_id"])
    assert "background runtime cap" in (task["result_text"] or "")
