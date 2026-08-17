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
    # PERMISSIONS_PATH is resolved at import time, so without this the new
    # launch-policy gate would read the DEVELOPER's live permissions.json and
    # a permissive local mode would auto-launch what these tests expect to
    # preview.
    from server.security import permissions as P

    monkeypatch.setattr(P, "PERMISSIONS_PATH", str(tmp_path / "permissions.json"))
    # A launched run now mirrors into the background-task registry, which
    # resolves its own DB_PATH at import time — without this, every run these
    # tests start leaves a phantom "Workflow: noop" task in the running
    # checkout's real sessions.db, visible in the developer's own app.
    from server.tasks import registry

    monkeypatch.setattr(registry, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(registry, "DB_PATH", str(storage / "sessions.db"))
    from server.workflows import launch_policy, manager

    manager._live.clear()
    manager._task_ids.clear()
    # Session grants are process-global; a launch in one test (POST /runs
    # grants its script hash) must not auto-launch a preview-expecting test.
    launch_policy._session_grants.clear()
    yield
    manager._live.clear()
    manager._task_ids.clear()
    launch_policy._session_grants.clear()


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
    # The launch route creates the run row and returns its id immediately. This
    # is also a regression test for TestClient portal shutdown: a detached task
    # must not keep the short-lived request loop open after the response.
    c = _client()
    r = c.post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "running"

    got = c.get(f"/api/workflows/runs/{run_id}")
    assert got.status_code == 200
    assert got.json()["run_id"] == run_id

    from server.workflows import manager

    runs = manager.list_runs("s1")
    assert any(x["run_id"] == run_id and x["name"] == "noop" for x in runs)

    assert c.get("/api/workflows/runs/nonexistent").status_code == 404


def test_route_launch_bad_script_400():
    c = _client()
    r = c.post("/api/workflows/runs", json={"script": "garbage", "session_id": "s1"})
    assert r.status_code == 400


# ── workflow category-mode launch gate ───────────────────────────────────────


def _perm(monkeypatch, mode="default", workflow_mode=None):
    from server.security import permissions as P

    data = {
        "mode": mode,
        "rules": [],
        "category_modes": {"workflow": workflow_mode} if workflow_mode else {},
    }
    monkeypatch.setattr(P, "load_permissions", lambda: data)


def test_launch_decision_modes(monkeypatch):
    from server.workflows.launch_policy import WORKFLOW_AUTO_MAX_TOKENS, launch_decision

    _perm(monkeypatch, workflow_mode="bypassPermissions")
    assert launch_decision(10**9) == "auto"

    _perm(monkeypatch, workflow_mode="auto")
    assert launch_decision(WORKFLOW_AUTO_MAX_TOKENS) == "auto"
    assert launch_decision(WORKFLOW_AUTO_MAX_TOKENS + 1) == "ask"

    _perm(monkeypatch, workflow_mode="dontAsk")
    assert launch_decision(1) == "deny"

    _perm(monkeypatch)  # global default, no category entry
    assert launch_decision(1) == "ask"

    # No category entry: the global mode is the fallback.
    _perm(monkeypatch, mode="bypassPermissions")
    assert launch_decision(10**9) == "auto"

    # A stricter category entry overrides a permissive global mode.
    _perm(monkeypatch, mode="bypassPermissions", workflow_mode="default")
    assert launch_decision(1) == "ask"


def test_workflow_run_auto_mode_launches_without_card(monkeypatch):
    import json

    from server.workflows.tools import execute_workflow_run

    _perm(monkeypatch, workflow_mode="auto")
    out, side = asyncio.run(execute_workflow_run({"script": _NOOP}, "s1", "", None))
    body = json.loads(out)
    assert body["auto_approved"] is True and body["run_id"]
    assert side and "workflow_started" in side[0]


def test_workflow_run_dontask_declines(monkeypatch):
    from server.workflows.tools import execute_workflow_run

    _perm(monkeypatch, workflow_mode="dontAsk")
    out, side = asyncio.run(execute_workflow_run({"script": _NOOP}, "s1", "", None))
    assert "not started" in out.lower() and side == []


def test_workflow_run_over_threshold_still_cards(monkeypatch):
    from server.workflows.launch_policy import WORKFLOW_AUTO_MAX_TOKENS
    from server.workflows.tools import execute_workflow_run

    _perm(monkeypatch, workflow_mode="auto")
    out, side = asyncio.run(
        execute_workflow_run(
            {"script": _NOOP, "budget_tokens": WORKFLOW_AUTO_MAX_TOKENS + 1}, "s1", "", None
        )
    )
    assert side and "workflow_preview" in side[0]
    assert side[0]["workflow_preview"]["budget_tokens"] == WORKFLOW_AUTO_MAX_TOKENS + 1


def test_permissions_endpoint_lists_workflow_category():
    from server.security import permissions as P

    app = FastAPI()
    app.include_router(P.router)
    cats = TestClient(app).get("/api/permissions").json()["categories"]
    assert "workflow" in cats


def test_route_launch_approval_trusts_a_matching_saved_workflow():
    # Approving IS trusting: when the approved bytes are a saved workflow,
    # the launch trusts it durably — there is no separate trust step.
    from server.workflows import store

    store.save_script("noop", _NOOP, {"name": "noop", "phases": ["a"]})
    c = _client()
    r = c.post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1", "name": "noop"})
    assert r.status_code == 200
    assert r.json()["trusted_as"] == "noop"
    assert store.load_script("noop")["trusted"] is True

    # An approved script that is NOT saved anywhere trusts nothing durably
    # (the session grant covers it) and never invents a library entry.
    other = _NOOP.replace("noop", "other")
    r2 = c.post("/api/workflows/runs", json={"script": other, "session_id": "s1"})
    assert r2.json()["trusted_as"] is None
    assert store.load_script("other") is None


def test_route_launch_never_trusts_different_bytes_under_a_shared_name():
    # An inline script that merely SHARES a saved workflow's name must not
    # trust bytes the user did not approve for that entry.
    from server.workflows import store

    store.save_script("noop", _NOOP.replace("{ ok: 1 }", "{ ok: 99 }"), {"name": "noop"})
    r = _client().post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"})
    assert r.status_code == 200
    assert r.json()["trusted_as"] is None
    assert store.load_script("noop")["trusted"] is False


def test_slugify_meta_names():
    from server.workflows import store

    assert store.slugify("Docs Refresh! v2") == "docs-refresh-v2"
    assert store.slugify("../evil") == "evil"  # traversal chars cannot survive
    assert store.slugify("???") == ""


def test_saved_rest_surface_is_gone_and_delete_tool_replaces_it():
    # The Settings library UI was removed, and its REST surface with it; the
    # store is managed from chat via workflow_save/workflow_list/workflow_delete.
    from server.workflows import store
    from server.workflows.tools import execute_workflow_delete

    store.save_script("wf", _NOOP, {"description": "d", "phases": ["a"]})
    c = _client()
    assert c.get("/api/workflows/saved").status_code in (404, 405)
    assert c.post("/api/workflows/saved/wf/approve").status_code in (404, 405)

    assert "Deleted workflow 'wf'" in execute_workflow_delete({"name": "wf"})
    assert store.load_script("wf") is None
    assert "No saved workflow" in execute_workflow_delete({"name": "wf"})


# ── pool + directive wiring ──────────────────────────────────────────────────


def test_ultracode_gates_tool_pool_and_core():
    from server.chat.tool_partition import core_names
    from server.chat.tool_pool import assemble_full_catalog

    on = {t["name"] for t in assemble_full_catalog(ultracode=True)}
    off = {t["name"] for t in assemble_full_catalog(ultracode=False)}
    assert "workflow_run" in on and "workflow_run" not in off
    # Workflow tools are core so progressive disclosure never defers them.
    assert {"workflow_run", "workflow_status"} <= core_names()
