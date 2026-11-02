"""WS-J slice 3: CI tool executors + REST routes + pool/partition wiring.

No network, no gh: provider/manager/autofix are monkeypatched. Verifies the
autofix tool reuses D's workflow_preview card, watch returns a task + card, and
the tools are surfaced only in ultracode mode.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.ci import manager, provider, tools


def _run(coro):
    return asyncio.run(coro)


# ── executors ────────────────────────────────────────────────────────────
def test_ci_status_executor(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(manager, "resolve_target", lambda b: (b, "/repo"))
    monkeypatch.setattr(
        manager, "status_snapshot", lambda b, c: {"available": True, "branch": b, "run": None}
    )
    out = _run(tools.execute_ci_status({"branch": "feat/x"}, "s1"))
    assert '"branch": "feat/x"' in out


def test_ci_status_no_gh(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: False)
    out = _run(tools.execute_ci_status({"branch": "b"}, "s1"))
    assert "gh" in out.lower()


def test_ci_watch_executor(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(manager, "resolve_target", lambda b: ("feat/x", "/repo"))
    monkeypatch.setattr(manager, "start_watch", lambda b, c, s: "task123")
    out, side = _run(tools.execute_ci_watch({}, "s1"))
    assert '"task_id": "task123"' in out
    assert side == [{"ci_started": {"task_id": "task123", "branch": "feat/x"}}]


def test_ci_autofix_emits_workflow_preview(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(manager, "resolve_target", lambda b: ("feat/x", "/repo"))
    run = {"run_id": 9, "status": "completed", "conclusion": "failure", "branch": "feat/x"}
    monkeypatch.setattr(provider, "latest_run", lambda *a, **k: run)
    monkeypatch.setattr(provider, "get_run", lambda *a, **k: run)
    monkeypatch.setattr(provider, "is_terminal", lambda r: True)
    monkeypatch.setattr(provider, "is_failing", lambda r: True)
    monkeypatch.setattr(
        tools.autofix,
        "plan_autofix",
        lambda *a, **k: {
            "run_id": 9,
            "branch": "feat/x",
            "url": "u",
            "summary": "1 finding",
            "findings": [{"check": "Backend", "category": "test", "summary": "s"}],
            "script": "export const meta = { name: 'ci-autofix' }\n",
        },
    )
    out, side = _run(tools.execute_ci_autofix({}, "s1", "model-x"))
    keys = [next(iter(s)) for s in side]
    assert "ci_diagnosis" in keys and "workflow_preview" in keys
    preview = next(s["workflow_preview"] for s in side if "workflow_preview" in s)
    assert preview["name"] == "ci-autofix" and preview["model_id"] == "model-x"


def test_ci_autofix_skips_when_not_failing(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(manager, "resolve_target", lambda b: ("feat/x", "/repo"))
    run = {"run_id": 9, "status": "completed", "conclusion": "success"}
    monkeypatch.setattr(provider, "latest_run", lambda *a, **k: run)
    monkeypatch.setattr(provider, "is_terminal", lambda r: True)
    monkeypatch.setattr(provider, "is_failing", lambda r: False)
    out, side = _run(tools.execute_ci_autofix({}, "s1", "m"))
    assert side == [] and "nothing to autofix" in out.lower()


# ── routes ───────────────────────────────────────────────────────────────
def _client():
    from server.ci.routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_route_status_no_gh(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: False)
    monkeypatch.setattr(manager, "resolve_target", lambda b: (b, "/repo"))
    r = _client().get("/api/ci/status?branch=b")
    assert r.status_code == 200 and r.json()["available"] is False


def test_route_watch_starts(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(manager, "resolve_target", lambda b: ("feat/x", "/repo"))
    monkeypatch.setattr(manager, "start_watch", lambda b, c, s: "t9")
    r = _client().post("/api/ci/watch", json={"branch": "feat/x", "session_id": "s1"})
    assert r.status_code == 200 and r.json()["task_id"] == "t9"


def test_route_autofix_not_failing(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(manager, "resolve_target", lambda b: ("feat/x", "/repo"))
    monkeypatch.setattr(provider, "latest_run", lambda *a, **k: {"run_id": 1})
    monkeypatch.setattr(provider, "is_failing", lambda r: False)
    r = _client().post("/api/ci/autofix", json={"branch": "feat/x"})
    assert r.status_code == 200 and r.json()["script"] is None


# ── wiring ───────────────────────────────────────────────────────────────
def test_tools_ultracode_only():
    from server.chat.tool_partition import core_names
    from server.chat.tool_pool import assemble_full_catalog

    on = {t["name"] for t in assemble_full_catalog(ultracode=True)}
    off = {t["name"] for t in assemble_full_catalog(ultracode=False)}
    ci = {"ci_watch", "ci_status", "ci_autofix"}
    assert ci <= on
    assert not (ci & off)
    assert ci <= core_names()


def test_tool_router_dispatches_ci(monkeypatch):
    # The dispatch branches exist and forward to the executors.
    import server.tool_router as tr

    src = tr.__file__
    with open(src) as f:
        body = f.read()
    for name in ("ci_watch", "ci_status", "ci_autofix"):
        assert f'tool_name == "{name}"' in body


# ── no repository to ask about, or gh failing ─────────────────────────────
def test_no_workspace_is_an_error_not_the_server_folder(monkeypatch):
    """CI used to run gh in the server process's own folder when no
    workspace was connected and report that repo's runs."""
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    with pytest.raises(manager.CITargetError, match="No workspace connected"):
        manager.resolve_target("")
    out = _run(tools.execute_ci_status({}, "s1"))
    assert out.startswith("Error: No workspace connected")
    r = _client().get("/api/ci/status")
    assert r.status_code == 400 and "No workspace connected" in r.json()["error"]


def test_a_detached_head_needs_a_branch(monkeypatch, tmp_path):
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr("server.git.core.find_git_root", lambda p: p)
    monkeypatch.setattr("server.git.core.get_branch", lambda p: "HEAD")
    with pytest.raises(manager.CITargetError, match="detached"):
        manager.resolve_target("")
    assert manager.resolve_target("main") == ("main", str(tmp_path))


def test_a_gh_failure_reaches_the_tool_and_the_route(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(manager, "resolve_target", lambda b: ("feat/x", "/repo"))

    def fail(*a, **k):
        raise provider.GhError("gh run list failed (exit 4): gh auth login")

    monkeypatch.setattr(provider, "latest_run", fail)
    out, side = _run(tools.execute_ci_autofix({}, "s1", "m"))
    assert out == "Error: gh run list failed (exit 4): gh auth login" and side == []
    r = _client().post("/api/ci/autofix", json={})
    assert r.status_code == 502 and "gh auth login" in r.json()["error"]
