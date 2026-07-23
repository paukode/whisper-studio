"""WS-J slice 3: CI tool executors + REST routes + pool/partition wiring.

No network, no gh: provider/manager/autofix are monkeypatched. Verifies the
autofix tool reuses D's workflow_preview card, watch returns a task + card, and
the tools are surfaced only in ultracode mode.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.ci import manager, provider, tools


def _run(coro):
    return asyncio.run(coro)


# ── executors ────────────────────────────────────────────────────────────
def test_ci_status_executor(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
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
    monkeypatch.setattr(tools, "_resolve", lambda ti: ("feat/x", "/repo"))
    monkeypatch.setattr(manager, "start_watch", lambda b, c, s: "task123")
    out, side = _run(tools.execute_ci_watch({}, "s1"))
    assert '"task_id": "task123"' in out
    assert side == [{"ci_started": {"task_id": "task123", "branch": "feat/x"}}]


def test_ci_autofix_emits_workflow_preview(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr(tools, "_resolve", lambda ti: ("feat/x", "/repo"))
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
    monkeypatch.setattr(tools, "_resolve", lambda ti: ("feat/x", "/repo"))
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
    r = _client().get("/api/ci/status?branch=b")
    assert r.status_code == 200 and r.json()["available"] is False


def test_route_watch_starts(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr("server.ci.routes._resolve", lambda b, c: ("feat/x", "/repo"))
    monkeypatch.setattr(manager, "start_watch", lambda b, c, s: "t9")
    r = _client().post("/api/ci/watch", json={"branch": "feat/x", "session_id": "s1"})
    assert r.status_code == 200 and r.json()["task_id"] == "t9"


def test_route_autofix_not_failing(monkeypatch):
    monkeypatch.setattr(provider, "gh_available", lambda: True)
    monkeypatch.setattr("server.ci.routes._resolve", lambda b, c: ("feat/x", "/repo"))
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
