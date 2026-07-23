"""Unified background-task registry: CRUD, prune, and boot reconcile."""

import os
import subprocess
import time

import pytest

from server.tasks import registry


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    yield


def test_create_get_roundtrip():
    tid = registry.create_task(
        "shell",
        session_id="s1",
        title="sleep 5",
        command="sleep 5",
        output_path="/tmp/x.txt",
        meta={"cwd": "/tmp"},
    )
    task = registry.get_task(tid)
    assert task["kind"] == "shell"
    assert task["session_id"] == "s1"
    assert task["status"] == "running"
    assert task["command"] == "sleep 5"
    assert task["meta"] == {"cwd": "/tmp"}
    assert task["owner_pid"] == os.getpid()
    assert task["exit_code"] is None


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        registry.create_task("cron", title="nope")


def test_finish_records_terminal_state_and_is_idempotent():
    tid = registry.create_task("shell", title="t", session_id="s1")
    finished = registry.finish_task(tid, status="completed", exit_code=0, result_text="done")
    assert finished["status"] == "completed"
    assert finished["exit_code"] == 0
    assert finished["finished_at"]
    # A second terminal transition (e.g. stop racing natural exit) is a no-op.
    again = registry.finish_task(tid, status="stopped", result_text="late stop")
    assert again is None
    assert registry.get_task(tid)["status"] == "completed"


def test_finish_rejects_non_terminal_status():
    tid = registry.create_task("agent", title="a")
    with pytest.raises(ValueError):
        registry.finish_task(tid, status="running")


def test_list_filters():
    a = registry.create_task("shell", session_id="s1", title="one")
    b = registry.create_task("agent", session_id="s2", title="two")
    registry.finish_task(b, status="failed", result_text="boom")
    assert {t["task_id"] for t in registry.list_tasks(session_id="s1")} == {a}
    assert {t["task_id"] for t in registry.list_tasks(status="running")} == {a}
    assert {t["task_id"] for t in registry.list_tasks(kind="agent")} == {b}
    assert len(registry.list_tasks()) == 2


def test_prune_caps_finished_backlog(monkeypatch):
    monkeypatch.setattr(registry, "MAX_FINISHED", 3)
    keep_running = registry.create_task("shell", title="live")
    finished_ids = []
    for i in range(6):
        tid = registry.create_task("shell", title=f"f{i}")
        registry.finish_task(tid, status="completed", exit_code=0)
        finished_ids.append(tid)
        time.sleep(0.01)  # distinct finished_at ordering
    remaining = registry.list_tasks(limit=100)
    remaining_ids = {t["task_id"] for t in remaining}
    assert keep_running in remaining_ids  # running rows never pruned
    finished_remaining = [t for t in remaining if t["status"] != "running"]
    assert len(finished_remaining) == 3
    assert {t["task_id"] for t in finished_remaining} == set(finished_ids[-3:])


def test_reconcile_marks_foreign_agent_rows_interrupted():
    tid = registry.create_task("agent", session_id="s1", title="agent work")
    with registry._get_conn() as conn:
        conn.execute("UPDATE agent_tasks SET owner_pid=? WHERE task_id=?", (999999, tid))
    counts = registry.reconcile_on_boot()
    assert counts["interrupted"] == 1
    task = registry.get_task(tid)
    assert task["status"] == "interrupted"
    assert "[interrupted]" in task["result_text"]


def test_reconcile_dead_shell_pid_interrupted_with_tail(tmp_path):
    out = tmp_path / "out.txt"
    out.write_text("line1\nline2\n")
    tid = registry.create_task("shell", title="dead", output_path=str(out))
    # A freshly-reaped child pid is guaranteed dead.
    proc = subprocess.Popen(["true"])
    proc.wait()
    with registry._get_conn() as conn:
        conn.execute(
            "UPDATE agent_tasks SET owner_pid=?, pid=? WHERE task_id=?",
            (999999, proc.pid, tid),
        )
    counts = registry.reconcile_on_boot()
    assert counts == {"interrupted": 1, "adopted": 0}
    task = registry.get_task(tid)
    assert task["status"] == "interrupted"
    assert "line2" in task["result_text"]


def test_reconcile_live_shell_pid_adopted_then_finished(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_ADOPT_POLL_S", 0.05)
    out = tmp_path / "out.txt"
    out.write_text("adopted output\n")
    proc = subprocess.Popen(["sleep", "0.4"])
    tid = registry.create_task("shell", title="survivor", output_path=str(out))
    with registry._get_conn() as conn:
        conn.execute(
            "UPDATE agent_tasks SET owner_pid=?, pid=? WHERE task_id=?",
            (999999, proc.pid, tid),
        )
    counts = registry.reconcile_on_boot()
    assert counts == {"interrupted": 0, "adopted": 1}
    assert registry.get_task(tid)["status"] == "running"  # still alive: re-adopted
    proc.wait()
    deadline = time.time() + 5
    while time.time() < deadline and registry.get_task(tid)["status"] == "running":
        time.sleep(0.05)
    task = registry.get_task(tid)
    assert task["status"] == "completed"
    assert task["exit_code"] is None  # honestly unknowable after re-parenting
    assert "adopted after server restart" in task["result_text"]
    assert "adopted output" in task["result_text"]


def test_reconcile_ignores_own_rows():
    tid = registry.create_task("shell", title="mine")
    counts = registry.reconcile_on_boot()
    assert counts == {"interrupted": 0, "adopted": 0}
    assert registry.get_task(tid)["status"] == "running"


def test_pid_alive_treats_eperm_as_dead(monkeypatch):
    """EPERM means a different uid owns the pid — never one of our children,
    so for adoption purposes it must read as dead (pid-reuse hazard)."""

    def _kill_eperm(pid, sig):
        raise PermissionError

    monkeypatch.setattr(registry.os, "kill", _kill_eperm)
    assert registry._pid_alive(12345) is False


def test_reconcile_no_adoption_after_system_reboot(tmp_path, monkeypatch):
    """A live-looking pid must NOT be adopted when the machine rebooted after
    the row was created: the original child cannot have survived."""
    proc = subprocess.Popen(["sleep", "5"])
    tid = registry.create_task("shell", title="pre-reboot", output_path=None)
    with registry._get_conn() as conn:
        conn.execute(
            "UPDATE agent_tasks SET owner_pid=?, pid=? WHERE task_id=?",
            (999999, proc.pid, tid),
        )
    monkeypatch.setattr(registry, "_system_boot_time", lambda: time.time() + 10)
    counts = registry.reconcile_on_boot()
    proc.kill()
    proc.wait()
    assert counts == {"interrupted": 1, "adopted": 0}
    assert registry.get_task(tid)["status"] == "interrupted"


def test_reconcile_skips_rows_owned_by_live_server(monkeypatch):
    """A row whose owning server process is still alive belongs to that
    process (multi-instance dev) — reconcile must leave it alone."""
    owner = subprocess.Popen(["sleep", "5"])
    tid = registry.create_task("agent", title="other server's work")
    with registry._get_conn() as conn:
        conn.execute("UPDATE agent_tasks SET owner_pid=? WHERE task_id=?", (owner.pid, tid))
    counts = registry.reconcile_on_boot()
    owner.kill()
    owner.wait()
    assert counts == {"interrupted": 0, "adopted": 0}
    assert registry.get_task(tid)["status"] == "running"


def test_tail_lines_of_file_bounded(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("".join(f"line{i}\n" for i in range(1000)))
    out = registry.tail_lines_of_file(str(p), 3)
    assert out == "line997\nline998\nline999\n"
    # Byte cap respected: a tiny max_bytes returns at most that much data.
    capped = registry.tail_lines_of_file(str(p), 500, max_bytes=64)
    assert len(capped.encode()) <= 64
    assert registry.tail_lines_of_file(None, 5) == ""
