"""The anti-restart proof for the foreground->background handoff.

The old auto-background flow killed the process at the 30s budget and
re-ran the command from scratch, discarding the first attempt's work and
re-running its side effects. run_with_handoff must start the process exactly
once and keep the SAME process running across the handoff.
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


def _wait_status(task_id: str, status: str, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = registry.get_task(task_id)
        if task and task["status"] == status:
            return True
        time.sleep(0.05)
    return False


def test_handoff_executes_exactly_once(tmp_path):
    """A command slower than the budget appends a start marker ONCE, is handed
    off live, and completes with exactly one marker — the restart bug's tomb."""
    marker = tmp_path / "starts.txt"
    cmd = f"echo started >> {marker} && sleep 1.2 && echo finished-cleanly"
    result = handoff.run_with_handoff(cmd, cmd, cwd=str(tmp_path), session_id="s-once", timeout=0.4)
    assert result.background is True
    assert result.task_id

    assert _wait_status(result.task_id, "completed")
    # THE assertion: one start marker means one execution across the handoff.
    assert marker.read_text().count("started") == 1
    task = registry.get_task(result.task_id)
    assert task["exit_code"] == 0
    assert "finished-cleanly" in task["result_text"]
    # Output file continuity: pre-handoff output survived the transition.
    with open(result.output_path) as f:
        assert "finished-cleanly" in f.read()


def test_fast_path_returns_inline_with_no_registry_row(tmp_path):
    result = handoff.run_with_handoff(
        "echo quick && exit 7", "echo quick && exit 7", cwd=str(tmp_path), timeout=10
    )
    assert result.background is False
    assert result.returncode == 7
    assert "quick" in result.output
    # No trace left behind: no registry row, and the output dir is empty.
    assert registry.list_tasks(limit=50) == []
    import os

    leftover = [f for f in os.listdir(shell.OUTPUT_DIR) if f.endswith(".txt")]
    assert leftover == []


def test_handoff_task_row_carries_command_and_meta(tmp_path):
    cmd = "sleep 1"
    result = handoff.run_with_handoff(cmd, cmd, cwd=str(tmp_path), session_id="s2", timeout=0.2)
    task = registry.get_task(result.task_id)
    assert task["command"] == "sleep 1"
    assert task["session_id"] == "s2"
    assert task["meta"]["handoff"] is True
    assert task["pid"]
    _wait_status(result.task_id, "completed")


def test_handed_off_task_stoppable_via_esc_path(tmp_path):
    cmd = "sleep 30"
    result = handoff.run_with_handoff(cmd, cmd, cwd=str(tmp_path), session_id="s3", timeout=0.2)
    assert result.background is True
    stopped = shell.stop_session_tasks("s3")
    assert result.task_id in stopped
    assert _wait_status(result.task_id, "stopped")


def test_adopted_task_stoppable_via_registry_pid_fallback(tmp_path):
    """A task re-adopted after a server restart has no Popen handle; stop_task
    must fall back to the registry row's process-group-leader pid, kill the
    group, and close the row as 'stopped' first-wins."""
    import os
    import subprocess

    proc = subprocess.Popen(["sleep", "30"], preexec_fn=os.setpgrp)
    out = tmp_path / "adopted.txt"
    out.write_text("some output\n")
    tid = registry.create_task(
        "shell", session_id="s-adopt", title="sleep 30", output_path=str(out)
    )
    registry.attach_pid(tid, proc.pid)
    # No adopt_running_process call: simulates the post-restart state where
    # only the registry row exists (shell._procs is empty for this task).
    assert shell.stop_task(tid) is True
    proc.wait()
    task = registry.get_task(tid)
    assert task["status"] == "stopped"
    assert "some output" in task["result_text"]


def test_stop_session_covers_adopted_tasks(tmp_path):
    import os
    import subprocess

    proc = subprocess.Popen(["sleep", "30"], preexec_fn=os.setpgrp)
    tid = registry.create_task("shell", session_id="s-esc2", title="sleep 30")
    registry.attach_pid(tid, proc.pid)
    stopped = shell.stop_session_tasks("s-esc2")
    proc.wait()
    assert tid in stopped
    assert registry.get_task(tid)["status"] == "stopped"
