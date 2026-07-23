"""WS-D slice 4: the model-facing tools, the HTTP router, and pool/directive
wiring. Uses agent-free scripts so no Bedrock is touched.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

NODE = shutil.which("node") or "/usr/local/bin/node"
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "server", "workflows", "harness", "harness.mjs"
    )
)
pytestmark = pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")

_NOOP = "export const meta = { name: 'noop', description: 'no agents', phases: ['a'] }\nphase('a')\nreturn { ok: 1 }\n"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    from server.workflows import manager

    manager._live.clear()
    yield
    manager._live.clear()


def _client():
    from server.workflows.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── tools ────────────────────────────────────────────────────────────────────


def test_workflow_run_new_script_returns_preview():
    from server.workflows.tools import execute_workflow_run

    src = "export const meta = { name: 'demo', description: 'd', phases: ['x','y'] }\nreturn 1\n"
    out, side = asyncio.run(execute_workflow_run({"script": src}, "s1", "", None))
    assert "approval" in out.lower()
    assert side and "workflow_preview" in side[0]
    assert side[0]["workflow_preview"]["phases"] == ["x", "y"]


def test_workflow_run_bad_script_errors():
    from server.workflows.tools import execute_workflow_run

    out, side = asyncio.run(execute_workflow_run({"script": "no meta here"}, "s1", "", None))
    assert "error" in out.lower() and side == []


def test_workflow_save_and_list():
    from server.workflows.tools import execute_workflow_list, execute_workflow_save

    msg = asyncio.run(execute_workflow_save({"name": "my-flow", "script": _NOOP}))
    assert "Saved workflow 'my-flow'" in msg
    listing = execute_workflow_list({}, "s1")
    assert "my-flow" in listing


def test_workflow_save_rejects_bad_name():
    from server.workflows.tools import execute_workflow_save

    msg = asyncio.run(execute_workflow_save({"name": "../evil", "script": _NOOP}))
    assert "Error" in msg


# ── routes ───────────────────────────────────────────────────────────────────


def test_route_launch_list_get():
    # The launch route creates the run row and returns its id immediately; the
    # detached run itself executes on the server loop (covered end-to-end by the
    # manager/runtime tests — TestClient's portal can't keep the background task
    # alive between requests).
    c = _client()
    r = c.post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "running"

    got = c.get(f"/api/workflows/runs/{run_id}")
    assert got.status_code == 200
    assert got.json()["run_id"] == run_id

    runs = c.get("/api/workflows/runs?session_id=s1").json()["runs"]
    assert any(x["run_id"] == run_id and x["name"] == "noop" for x in runs)

    assert c.get("/api/workflows/runs/nonexistent").status_code == 404


def test_route_launch_bad_script_400():
    c = _client()
    r = c.post("/api/workflows/runs", json={"script": "garbage", "session_id": "s1"})
    assert r.status_code == 400


def test_route_saved_crud_and_trust():
    from server.workflows import store

    store.save_script("wf", _NOOP, {"description": "d", "phases": ["a"]})
    c = _client()
    saved = c.get("/api/workflows/saved").json()["saved"]
    assert any(s["name"] == "wf" and s["trusted"] is False for s in saved)

    assert c.post("/api/workflows/saved/wf/approve").json()["approved"] is True
    assert c.get("/api/workflows/saved/wf").json()["trusted"] is True

    assert c.delete("/api/workflows/saved/wf").json()["deleted"] is True
    assert c.get("/api/workflows/saved/wf").status_code == 404


# ── pool + directive wiring ──────────────────────────────────────────────────


def test_ultracode_gates_tool_pool_and_core():
    from server.chat.tool_partition import core_names
    from server.chat.tool_pool import assemble_full_catalog

    on = {t["name"] for t in assemble_full_catalog(ultracode=True)}
    off = {t["name"] for t in assemble_full_catalog(ultracode=False)}
    assert "workflow_run" in on and "workflow_run" not in off
    # Workflow tools are core so progressive disclosure never defers them.
    assert {"workflow_run", "workflow_status"} <= core_names()
