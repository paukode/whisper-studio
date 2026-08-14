"""Report items #2 and #3: a workflow run must be pinned to a real, explicit
workspace root at launch, frozen for the whole run — a switch to a different
workspace while a run is in flight must not redirect its later agents, and
naming an arbitrary folder in the prompt must actually bind the run to it.

Covers: resolve_workflow_workspace (explicit/omitted/invalid), start_run
persisting and freezing the root, the model-facing tool's preview/launch/
resume paths, the REST approval route re-validating (never re-deriving) the
card's value, and run_agent forking a worktree from — or setting a plain
override to — the pinned root instead of the ambient global workspace.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

NODE = shutil.which("node") or "/usr/local/bin/node"
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "server", "workflows", "harness", "harness.mjs"
    )
)

_NOOP = "export const meta = { name: 'noop', description: 'no agents', phases: ['a'] }\nphase('a')\nreturn { ok: 1 }\n"


# ── resolve_workflow_workspace ───────────────────────────────────────────────


def test_resolve_explicit_path_expands_and_validates(tmp_path, monkeypatch):
    from server.workflows.manager import resolve_workflow_workspace

    real = tmp_path / "project1"
    real.mkdir()
    resolved, err = resolve_workflow_workspace(str(real))
    assert err == ""
    assert resolved == os.path.realpath(str(real))


def test_resolve_explicit_path_expands_tilde(tmp_path, monkeypatch):
    from server.workflows.manager import resolve_workflow_workspace

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads" / "project1").mkdir(parents=True)
    resolved, err = resolve_workflow_workspace("~/Downloads/project1")
    assert err == ""
    assert resolved == os.path.realpath(str(tmp_path / "Downloads" / "project1"))


def test_resolve_explicit_missing_path_errors_and_does_not_create_it(tmp_path):
    from server.workflows.manager import resolve_workflow_workspace

    missing = tmp_path / "does-not-exist"
    resolved, err = resolve_workflow_workspace(str(missing))
    assert resolved is None
    assert "does not exist" in err
    assert not missing.exists()  # never silently created


def test_resolve_omitted_snapshots_the_connected_workspace(tmp_path, monkeypatch):
    from server.workflows import manager

    real = tmp_path / "connected"
    real.mkdir()
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(real))
    resolved, err = manager.resolve_workflow_workspace(None)
    assert err == ""
    assert resolved == os.path.realpath(str(real))


def test_resolve_omitted_with_nothing_connected_returns_none_no_error(monkeypatch):
    from server.workflows import manager

    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    resolved, err = manager.resolve_workflow_workspace(None)
    assert resolved is None
    assert err == ""  # legacy fully-unpinned case, not a failure


# ── start_run persists and freezes the root ──────────────────────────────────


class _ManagerDBTestBase:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
        os.makedirs(tmp_path / "data", exist_ok=True)
        from server.infrastructure import sessions

        storage = tmp_path / "storage"
        os.makedirs(storage, exist_ok=True)
        monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
        monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
        from server.security import permissions as P

        monkeypatch.setattr(P, "PERMISSIONS_PATH", str(tmp_path / "permissions.json"))
        from server.tasks import registry

        monkeypatch.setattr(registry, "STORAGE_DIR", str(storage))
        monkeypatch.setattr(registry, "DB_PATH", str(storage / "sessions.db"))
        from server.workflows import manager

        manager._live.clear()
        manager._task_ids.clear()
        yield
        manager._live.clear()
        manager._task_ids.clear()


@pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")
class TestStartRunPersistsWorkspace(_ManagerDBTestBase):
    def test_start_run_stores_the_pinned_path(self, tmp_path):
        from server.workflows import manager

        ws = tmp_path / "pinned"
        ws.mkdir()
        run_id = manager.start_run(
            _NOOP, session_id="s1", model_key="sonnet", workspace_path=str(ws)
        )
        row = manager.get_run(run_id)
        assert row["workspace_path"] == str(ws)

    def test_start_run_with_no_workspace_stores_none(self):
        from server.workflows import manager

        run_id = manager.start_run(_NOOP, session_id="s1", model_key="sonnet")
        row = manager.get_run(run_id)
        assert row["workspace_path"] is None


# ── the model-facing tool: preview, auto-launch, resume ──────────────────────


@pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")
class TestExecuteWorkflowRunWorkspace(_ManagerDBTestBase):
    def test_explicit_workspace_path_reaches_the_preview_card(self, tmp_path):
        from server.workflows.tools import execute_workflow_run

        ws = tmp_path / "project1"
        ws.mkdir()
        out, side = asyncio.run(
            execute_workflow_run({"script": _NOOP, "workspace_path": str(ws)}, "s1", "", None)
        )
        assert "approval" in out.lower()
        assert side[0]["workflow_preview"]["workspace_path"] == os.path.realpath(str(ws))

    def test_explicit_missing_workspace_path_errors_before_any_preview(self, tmp_path):
        from server.workflows.tools import execute_workflow_run

        missing = tmp_path / "nope"
        out, side = asyncio.run(
            execute_workflow_run({"script": _NOOP, "workspace_path": str(missing)}, "s1", "", None)
        )
        assert "does not exist" in out
        assert side == []

    def test_omitted_workspace_path_snapshots_connected_workspace(self, tmp_path, monkeypatch):
        from server.workflows.tools import execute_workflow_run

        ws = tmp_path / "connected"
        ws.mkdir()
        monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
        out, side = asyncio.run(execute_workflow_run({"script": _NOOP}, "s1", "", None))
        assert side[0]["workflow_preview"]["workspace_path"] == os.path.realpath(str(ws))

    def test_auto_launch_stores_the_resolved_workspace(self, tmp_path, monkeypatch):
        """The auto-approved path (permissive workflow permission mode) must
        pin identically to the manual-approval path."""
        from server.security import permissions as P
        from server.workflows import manager
        from server.workflows.tools import execute_workflow_run

        monkeypatch.setattr(
            P,
            "load_permissions",
            lambda: {"mode": "default", "category_modes": {"workflow": "auto"}},
        )
        ws = tmp_path / "project1"
        ws.mkdir()
        out, side = asyncio.run(
            execute_workflow_run({"script": _NOOP, "workspace_path": str(ws)}, "s1", "", None)
        )
        assert side and "workflow_started" in side[0]
        run_id = side[0]["workflow_started"]["run_id"]
        row = manager.get_run(run_id)
        assert row["workspace_path"] == os.path.realpath(str(ws))

    def test_resume_reuses_the_original_runs_workspace_not_a_fresh_one(self, tmp_path, monkeypatch):
        from server.security import permissions as P
        from server.workflows import manager
        from server.workflows.tools import execute_workflow_run

        monkeypatch.setattr(
            P,
            "load_permissions",
            lambda: {"mode": "default", "category_modes": {"workflow": "auto"}},
        )
        original_ws = tmp_path / "original"
        original_ws.mkdir()
        _, side = asyncio.run(
            execute_workflow_run(
                {"script": _NOOP, "workspace_path": str(original_ws)}, "s1", "", None
            )
        )
        run_id = side[0]["workflow_started"]["run_id"]
        manager._live.pop(run_id, None)  # simulate a finished/stopped run

        # A DIFFERENT workspace is now connected — resume must ignore it.
        other_ws = tmp_path / "other"
        other_ws.mkdir()
        monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(other_ws))
        out2, side2 = asyncio.run(
            execute_workflow_run({"resume_from_run_id": run_id}, "s1", "", None)
        )
        resumed_id = side2[0]["workflow_started"]["run_id"]
        row = manager.get_run(resumed_id)
        assert row["workspace_path"] == os.path.realpath(str(original_ws))
        assert row["workspace_path"] != os.path.realpath(str(other_ws))


# ── the REST approval route ───────────────────────────────────────────────────


@pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")
class TestLaunchRouteWorkspace(_ManagerDBTestBase):
    def _client(self):
        from server.workflows.routes import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_launch_echoes_back_the_cards_workspace_path(self, tmp_path):
        from server.workflows import manager

        ws = tmp_path / "shown-on-card"
        ws.mkdir()
        c = self._client()
        r = c.post(
            "/api/workflows/runs",
            json={"script": _NOOP, "session_id": "s1", "workspace_path": str(ws)},
        )
        assert r.status_code == 200
        row = manager.get_run(r.json()["run_id"])
        assert row["workspace_path"] == os.path.realpath(str(ws))

    def test_launch_fails_if_the_cards_workspace_disappeared(self, tmp_path):
        """Never silently fall back to another workspace — the report's
        explicit fix requirement."""
        ws = tmp_path / "will-vanish"
        ws.mkdir()
        real = os.path.realpath(str(ws))
        os.rmdir(ws)

        c = self._client()
        r = c.post(
            "/api/workflows/runs",
            json={"script": _NOOP, "session_id": "s1", "workspace_path": real},
        )
        assert r.status_code == 400
        assert "does not exist" in r.json()["error"]

    def test_launch_without_a_card_value_pins_to_currently_connected(self, tmp_path, monkeypatch):
        """The /workflow <name> slash command sends no workspace_path at
        all — it must still get a pin, not run fully unpinned."""
        from server.workflows import manager, store

        ws = tmp_path / "connected-now"
        ws.mkdir()
        monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
        store.save_script("trusted-flow", _NOOP, {"name": "trusted-flow"}, trusted=True)
        c = self._client()
        r = c.post("/api/workflows/runs", json={"name": "trusted-flow", "session_id": "s1"})
        assert r.status_code == 200
        row = manager.get_run(r.json()["run_id"])
        assert row["workspace_path"] == os.path.realpath(str(ws))

    def test_card_echoing_an_empty_path_stays_unpinned_not_resnapshotted(
        self, tmp_path, monkeypatch
    ):
        """Adversarial-review regression: a card previewed with nothing
        connected legitimately sends `workspace_path: ""`. That must NOT be
        treated the same as "no card was involved" (which pins to whatever is
        connected right now) — the run must launch unpinned, exactly as the
        card showed, even if a workspace has since been connected."""
        from server.workflows import manager

        connected_after_preview = tmp_path / "connected-after-preview"
        connected_after_preview.mkdir()
        monkeypatch.setattr(
            "server.workspace.state.get_workspace_path", lambda: str(connected_after_preview)
        )
        c = self._client()
        r = c.post(
            "/api/workflows/runs",
            json={"script": _NOOP, "session_id": "s1", "workspace_path": ""},
        )
        assert r.status_code == 200
        row = manager.get_run(r.json()["run_id"])
        assert row["workspace_path"] is None
        assert row["workspace_path"] != os.path.realpath(str(connected_after_preview))


# ── run_agent: forking a worktree from, or pinning to, the run's root ────────


def _fake_git_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_run_agent_worktree_forks_from_the_pinned_workspace_not_the_global_one(
    monkeypatch, tmp_path
):
    """The heart of report item #3: a workflow's agent must isolate from the
    run's FROZEN root, never the workspace that happens to be globally
    connected by the time this particular agent starts."""
    from server.agents.runtime import AgentResult, run_agent

    pinned_repo = _fake_git_repo(tmp_path, "pinned-repo")
    global_repo = _fake_git_repo(tmp_path, "global-repo")  # connected AFTER launch
    monkeypatch.setattr(
        "server.workspace.state.load_workspace_config", lambda: {"path": str(global_repo)}
    )

    captured = {}

    async def _fake_loop(**kwargs):
        from server.workspace.state import get_workspace_path

        captured["ws"] = get_workspace_path()
        return AgentResult(agent_id="a1", agent_type="general", output="done")

    monkeypatch.setattr("server.agents.runtime._run_agent_loop", _fake_loop)

    result = asyncio.run(
        run_agent(
            "task",
            agent_type="general",
            session_id="sess-pin-1",
            model_id_override="test-model",
            isolation="worktree",
            workspace_path=str(pinned_repo),
        )
    )

    assert result.status == "completed"
    # The agent's own tool calls resolved against ITS worktree...
    assert "pinned-repo" in captured["ws"] or "agent-" in captured["ws"]
    assert "global-repo" not in captured["ws"]


def test_run_agent_worktree_creation_failure_still_pins_the_plain_override(monkeypatch, tmp_path):
    """Adversarial-review regression: a pinned root that is a valid directory
    but NOT a git repo makes _enter_agent_worktree return None. The agent
    degrades to read-only, but it must still resolve workspace tools against
    the PINNED root — not silently fall through to whatever is globally
    connected, which is exactly the bug isolation:"worktree" exists to
    prevent in the first place."""
    from server.agents.runtime import AgentResult, run_agent

    pinned_non_git = tmp_path / "pinned-non-git"
    pinned_non_git.mkdir()
    global_repo = _fake_git_repo(tmp_path, "global-repo-2")
    monkeypatch.setattr(
        "server.workspace.state.load_workspace_config", lambda: {"path": str(global_repo)}
    )

    captured = {}

    async def _fake_loop(**kwargs):
        from server.workspace.state import get_workspace_path

        captured["ws"] = get_workspace_path()
        return AgentResult(agent_id="a1", agent_type="general", output="done")

    monkeypatch.setattr("server.agents.runtime._run_agent_loop", _fake_loop)

    asyncio.run(
        run_agent(
            "task",
            agent_type="general",
            session_id="sess-pin-fallback",
            model_id_override="test-model",
            isolation="worktree",
            workspace_path=str(pinned_non_git),
        )
    )

    assert captured["ws"] == str(pinned_non_git)
    assert "global-repo-2" not in captured["ws"]


def test_run_agent_without_isolation_still_pins_the_plain_override(monkeypatch, tmp_path):
    """A workflow agent with no isolation:"worktree" opt must still resolve
    workspace tools against the run's pinned root, not whatever is globally
    connected."""
    from server.agents.runtime import AgentResult, run_agent

    pinned = tmp_path / "pinned-plain"
    pinned.mkdir()
    global_ws = tmp_path / "global-plain"
    global_ws.mkdir()
    monkeypatch.setattr(
        "server.workspace.state.load_workspace_config", lambda: {"path": str(global_ws)}
    )

    captured = {}

    async def _fake_loop(**kwargs):
        from server.workspace.state import get_workspace_path

        captured["ws"] = get_workspace_path()
        return AgentResult(agent_id="a1", agent_type="general", output="done")

    monkeypatch.setattr("server.agents.runtime._run_agent_loop", _fake_loop)

    asyncio.run(
        run_agent(
            "task",
            agent_type="general",
            session_id="sess-pin-2",
            model_id_override="test-model",
            isolation="none",
            workspace_path=str(pinned),
        )
    )
    assert captured["ws"] == str(pinned)


def test_run_agent_registers_and_clears_the_plain_pin_override(monkeypatch, tmp_path):
    """The same register/clear contract worktree isolation follows (PR #64) —
    a plain workspace_path pin must not permanently leak a cwd_tracker entry
    either."""
    from server.agents.runtime import AgentResult, run_agent

    pinned = tmp_path / "pinned-cleanup"
    pinned.mkdir()
    calls: list[tuple] = []
    monkeypatch.setattr(
        "server.cwd_tracker.register_override", lambda o: calls.append(("register", o))
    )
    monkeypatch.setattr(
        "server.cwd_tracker.clear_override", lambda s, o: calls.append(("clear", s, o))
    )
    monkeypatch.setattr(
        "server.agents.runtime._run_agent_loop",
        AsyncMock(return_value=AgentResult(agent_id="a1", agent_type="general", output="done")),
    )

    asyncio.run(
        run_agent(
            "task",
            agent_type="general",
            session_id="sess-pin-3",
            model_id_override="test-model",
            isolation="none",
            workspace_path=str(pinned),
        )
    )
    assert calls == [("register", str(pinned)), ("clear", "sess-pin-3", str(pinned))]


def test_enter_agent_worktree_accepts_an_explicit_repo_root(tmp_path):
    """Direct unit coverage of the primitive run_agent relies on: repo_root
    must win over whatever get_workspace_path() would otherwise return."""
    from server.agents.runtime import _enter_agent_worktree
    from server.workspace.state import reset_workspace_override, set_workspace_override

    explicit_repo = _fake_git_repo(tmp_path, "explicit")
    other_repo = _fake_git_repo(tmp_path, "ambient")

    token = set_workspace_override(str(other_repo))
    try:
        session = _enter_agent_worktree("wt1", "sess", repo_root=str(explicit_repo))
    finally:
        reset_workspace_override(token)

    assert session is not None
    assert os.path.exists(os.path.join(session.worktree_path, "f.txt"))
    subprocess.run(
        ["git", "worktree", "remove", "--force", session.worktree_path],
        cwd=explicit_repo,
        check=False,
    )
