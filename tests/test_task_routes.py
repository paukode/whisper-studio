"""REST surface for the unified background-task registry."""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.tasks import registry, shell
from server.tasks.routes import router


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_list_and_detail(client):
    a = registry.create_task("shell", session_id="s1", title="one", command="echo 1")
    b = registry.create_task("agent", session_id="s2", title="two")
    registry.finish_task(b, status="completed", result_text="done")

    r = client.get("/api/background-tasks")
    assert r.status_code == 200
    ids = {t["task_id"] for t in r.json()["tasks"]}
    assert ids == {a, b}
    assert all("owner_pid" not in t for t in r.json()["tasks"])

    r = client.get("/api/background-tasks?session_id=s1&status=running")
    assert [t["task_id"] for t in r.json()["tasks"]] == [a]

    r = client.get(f"/api/background-tasks/{a}")
    assert r.json()["command"] == "echo 1"
    assert client.get("/api/background-tasks/nope").status_code == 404


def test_output_endpoint(tmp_path, client):
    out = tmp_path / "o.txt"
    out.write_text("hello output\n")
    tid = registry.create_task("shell", session_id="s1", title="t", output_path=str(out))
    r = client.get(f"/api/background-tasks/{tid}/output")
    assert r.json()["output"] == "hello output\n"


def test_stop_endpoint_kills_shell_task(tmp_path, client):
    info = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="s1")
    time.sleep(0.2)
    r = client.post(f"/api/background-tasks/{info['task_id']}/stop")
    assert r.json()["stopped"] is True
    deadline = time.time() + 3
    while time.time() < deadline and registry.get_task(info["task_id"])["status"] == "running":
        time.sleep(0.05)
    assert registry.get_task(info["task_id"])["status"] == "stopped"


def test_stop_endpoint_on_finished_task(client):
    tid = registry.create_task("shell", session_id="s1", title="t")
    registry.finish_task(tid, status="completed", exit_code=0)
    r = client.post(f"/api/background-tasks/{tid}/stop")
    assert r.json() == {"stopped": False, "status": "completed"}


def test_stop_session_endpoint(tmp_path, client):
    info = shell.start_shell_task("sleep 30", cwd=str(tmp_path), session_id="s-esc")
    time.sleep(0.2)
    r = client.post("/api/background-tasks/stop-session", json={"session_id": "s-esc"})
    assert info["task_id"] in r.json()["stopped"]
    assert client.post("/api/background-tasks/stop-session", json={}).status_code == 400


def test_todo_tracker_routes_not_shadowed():
    """The /api/background-tasks prefix must not collide with the todo
    tracker's /api/tasks routes."""
    from server.tasks_tracker import router as todo_router

    todo_paths = {r.path for r in todo_router.routes}
    bg_paths = {r.path for r in router.routes}
    assert all(p.startswith("/api/background-tasks") for p in bg_paths)
    assert not (todo_paths & bg_paths)
