"""Pins for the dead-code sweep: the flag registry holds only flags that are
actually read, the deleted zero-caller routes stay deleted, and the ESC
kill-switch background-task stop flow works over real HTTP."""

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_flags_registry_holds_only_live_flags():
    from server.infrastructure.feature_flags import get_all_flags, router

    names = set(get_all_flags().keys())
    removed = {
        "brief_mode",
        "auto_mode",
        "context_compaction",
        "tool_result_budget",
        "transcript_injection",
        "whisper_md",
        "parallel_tools",
        "latched_config",
        "secret_scanning",
    }
    live = {
        "companion",
        "git_context",
        "prompt_caching",
        "progressive_tools",
        "strict_rag",
        "rag_hybrid_search",
        "rag_reranker",
        "rag_query_rewrite",
        "auto_memory",
        "session_memory",
        "dream_consolidation",
        "preview_tools",
        "goal_loop",
        "cron_verify",
    }
    assert names & removed == set(), f"no-op flags back in the registry: {names & removed}"
    assert names == live, f"registry drift: extra={names - live} missing={live - names}"

    app = FastAPI()
    app.include_router(router)
    api_names = set(TestClient(app).get("/api/feature-flags").json().keys())
    assert api_names == live


def test_deleted_routes_stay_deleted_and_kept_routes_remain():
    from server import workspace
    from server.cron_scheduler import router as cron_router
    from server.git.router import router as git_router
    from server.infrastructure.hooks import router as hooks_router
    from server.infrastructure.sessions import router as sessions_router
    from server.security.permissions import router as perm_router
    from server.workspace import routes as _  # noqa: F401 — populates the shared router

    def paths(router):
        return {(r.path, m) for r in router.routes for m in (r.methods or [])}

    ws = paths(workspace.router)
    assert ("/api/workspace/tree", "GET") not in ws
    assert ("/api/workspace/mode", "POST") not in ws
    assert ("/api/workspace/cli/execute", "POST") not in ws
    assert ("/api/workspace/shell/tasks", "GET") not in ws
    assert ("/api/workspace/shell/task/{task_id}", "GET") not in ws
    # Plan mode is driven through the permissions API; these dupes are gone.
    assert ("/api/workspace/plan-mode", "POST") not in ws
    assert ("/api/workspace/plan-mode", "GET") not in ws
    # The one kept-and-rewired route, plus the live shell endpoint.
    assert ("/api/workspace/shell/tasks/stop", "POST") in ws
    assert ("/api/workspace/shell", "POST") in ws

    # The localStorage-migration import endpoint was dead; it stays deleted.
    assert ("/api/sessions/import", "POST") not in paths(sessions_router)

    g = paths(git_router)
    assert not any("/log" in p or "/branches" in p or "/checkout" in p for p, _m in g)
    assert any(p.endswith("/show") for p, _m in g)  # kept — UI diff view uses it

    assert not any(p.endswith("/validate") for p, _m in paths(perm_router))
    assert not any(p.endswith("/fire") for p, _m in paths(hooks_router))

    # The deprecated name-based cron delete was dead; it stays deleted. The
    # id-based delete is the live path and remains.
    cron = paths(cron_router)
    assert ("/api/cron/by-name/{name}", "DELETE") not in cron
    assert ("/api/cron/{job_id}", "DELETE") in cron


def test_esc_stop_flow_end_to_end(tmp_path):
    """Full HTTP round-trip: connect workspace, background a sleep, ESC-stop it.

    Runs with the real shell-profile snapshot: wrap_command re-executes under
    the snapshot's own shell, so a zsh profile no longer aborts under the
    /bin/sh that background tasks use. The sleep must actually be running
    for the stop call to find it.
    """
    from server import workspace
    from server.workspace import routes as _  # noqa: F401

    app = FastAPI()
    app.include_router(workspace.router)
    c = TestClient(app)

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    r = c.post("/api/workspace/connect", json={"path": str(ws_dir)})
    assert r.status_code == 200, r.text
    try:
        r = c.post(
            "/api/workspace/shell",
            json={"command": "sleep 30", "background": True, "session_id": "esc-live"},
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]
        time.sleep(0.3)

        r = c.post("/api/workspace/shell/tasks/stop", json={"session_id": "esc-live"})
        assert r.status_code == 200, r.text
        assert task_id in r.json()["stopped"], r.text

        from server.tasks.registry import get_task

        deadline = time.time() + 3
        while time.time() < deadline and get_task(task_id)["status"] == "running":
            time.sleep(0.05)
        assert get_task(task_id)["status"] == "stopped"

        # Second stop for the same session: nothing left to kill.
        r = c.post("/api/workspace/shell/tasks/stop", json={"session_id": "esc-live"})
        assert r.json()["stopped"] == []
    finally:
        c.post("/api/workspace/disconnect")
