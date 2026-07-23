"""Contract for the ESC kill switch's background-task half.

killSessionStream (frontend) fires POST /api/workspace/shell/tasks/stop with
its session_id; stop_session_tasks must kill every RUNNING task that session
started and leave other sessions' tasks alone. Since the unified registry,
an ESC-killed task records the honest status 'stopped' (previously the
in-memory registry marked it 'completed').
"""

import time

import pytest

from server.tasks import registry, shell


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


def test_stop_session_tasks_kills_only_that_sessions_running_tasks(tmp_path):
    mine = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="sess-a")
    other = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="sess-b")
    time.sleep(0.3)

    stopped = shell.stop_session_tasks("sess-a")

    assert mine["task_id"] in stopped
    assert other["task_id"] not in stopped
    # The waiter observes the kill and records the honest terminal status.
    assert _wait_status(mine["task_id"], "stopped")
    assert registry.get_task(other["task_id"])["status"] == "running"
    shell.stop_session_tasks("sess-b")


def test_stop_session_tasks_handles_unknown_or_empty_session(tmp_path):
    assert shell.stop_session_tasks("no-such-session") == []
    assert shell.stop_session_tasks("") == []


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
