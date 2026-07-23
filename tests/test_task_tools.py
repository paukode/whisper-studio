"""Model-facing background-task tools: status, output, cancel."""

import json
import time

import pytest

from server.executors import EXECUTOR_META
from server.tasks import registry, shell
from server.tasks.tools import (
    BACKGROUND_TASK_TOOL_NAMES,
    execute_background_task_tool,
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    yield


def test_task_status_lists_session_and_running_elsewhere():
    mine = registry.create_task("shell", session_id="s1", title="one")
    theirs = registry.create_task("agent", session_id="s2", title="two")
    done = registry.create_task("shell", session_id="s2", title="three")
    registry.finish_task(done, status="completed", exit_code=0)

    out = json.loads(execute_background_task_tool("task_status", {}, "s1"))
    assert [t["task_id"] for t in out["session_tasks"]] == [mine]
    assert [t["task_id"] for t in out["running_elsewhere"]] == [theirs]


def test_task_status_specific_id_and_unknown():
    tid = registry.create_task("shell", session_id="s1", title="x", command="echo x")
    registry.finish_task(tid, status="failed", exit_code=2, result_text="boom")
    out = json.loads(execute_background_task_tool("task_status", {"task_id": tid}, "s1"))
    assert out["status"] == "failed"
    assert out["exit_code"] == 2
    assert out["result_tail"] == "boom"
    msg = execute_background_task_tool("task_status", {"task_id": "nope"}, "s1")
    assert "No background task" in msg


def test_task_output_tails_the_file(tmp_path):
    out_file = tmp_path / "o.txt"
    out_file.write_text("".join(f"line{i}\n" for i in range(300)))
    tid = registry.create_task("shell", session_id="s1", title="t", output_path=str(out_file))
    text = execute_background_task_tool("task_output", {"task_id": tid, "tail_lines": 5}, "s1")
    assert "line299" in text
    assert "line294" not in text.split("]\n", 1)[1].splitlines()[0]
    assert text.count("line") == 5


def test_task_output_falls_back_to_result_text():
    tid = registry.create_task("agent", session_id="s1", title="a")
    registry.finish_task(tid, status="completed", result_text="final answer")
    text = execute_background_task_tool("task_output", {"task_id": tid}, "s1")
    assert "final answer" in text


def test_task_cancel_stops_running_shell(tmp_path):
    info = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="s1")
    time.sleep(0.2)
    msg = execute_background_task_tool("task_cancel", {"task_id": info["task_id"]}, "s1")
    assert "Stop signal sent" in msg
    deadline = time.time() + 3
    while time.time() < deadline and registry.get_task(info["task_id"])["status"] == "running":
        time.sleep(0.05)
    assert registry.get_task(info["task_id"])["status"] == "stopped"


def test_task_cancel_rejects_finished():
    tid = registry.create_task("shell", session_id="s1", title="t")
    registry.finish_task(tid, status="completed", exit_code=0)
    msg = execute_background_task_tool("task_cancel", {"task_id": tid}, "s1")
    assert "not running" in msg


def test_read_only_metadata_keeps_status_output_strips_cancel():
    """Agents' read_only filter must keep status/output and strip cancel."""
    assert EXECUTOR_META["task_status"]["read_only"] is True
    assert EXECUTOR_META["task_output"]["read_only"] is True
    assert EXECUTOR_META["task_cancel"]["read_only"] is False
    assert BACKGROUND_TASK_TOOL_NAMES == {"task_status", "task_output", "task_cancel"}


def test_task_output_and_cancel_enforce_session_ownership(tmp_path):
    out = tmp_path / "o.txt"
    out.write_text("secret output\n")
    tid = registry.create_task("shell", session_id="owner-s", title="t", output_path=str(out))
    denied = execute_background_task_tool("task_output", {"task_id": tid}, "intruder-s")
    assert "belongs to another session" in denied
    denied = execute_background_task_tool("task_cancel", {"task_id": tid}, "intruder-s")
    assert "belongs to another session" in denied
    # The owner still passes.
    ok = execute_background_task_tool("task_output", {"task_id": tid}, "owner-s")
    assert "secret output" in ok
