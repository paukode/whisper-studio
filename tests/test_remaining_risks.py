"""The report's unreproduced risks, now investigated and either fixed or
pinned as a deliberate trade-off.

1. Stop-vs-finalization race — CONFIRMED, fixed: both writers set terminal
   state unconditionally, so a stopped run got relabelled "failed" by the
   natural completion landing a moment later.
2. Shared cwd among parallel agents — CONFIRMED for NON-isolated agents,
   fixed: worktree isolation separated agents by their unique worktree path,
   but siblings in a pinned workflow run share both session_id AND workspace
   path, so they shared one cwd slot.
3. Static per-agent tool catalogs — real but deliberately NOT changed; see
   test_workspace_tools_fail_safely_when_the_workspace_vanishes.
"""

import asyncio
import os

import pytest

from server.cwd_tracker import (
    _session_cwd,
    clear_scope,
    get_cwd,
    register_override,
    reset_cwd_scope,
    set_cwd_scope,
    update_cwd,
)


@pytest.fixture(autouse=True)
def _clean_tracker():
    _session_cwd.clear()
    yield
    _session_cwd.clear()


# ── 2. parallel agents must not share one cwd slot ──────────────────────────


def test_sibling_agents_in_one_run_do_not_share_a_cwd_slot(tmp_path):
    """Two NON-isolated agents in the same workflow run share a session_id
    and (since pinning) a workspace path. Without a per-agent scope, one
    agent's `cd` silently relocated the next agent's commands."""
    session_id = "wf-session"
    root = tmp_path / "repo"
    sub = root / "sub"
    sub.mkdir(parents=True)

    tok_a = set_cwd_scope("agent-a")
    try:
        update_cwd(session_id, str(sub))
        assert get_cwd(session_id, str(root)) == str(sub)
    finally:
        reset_cwd_scope(tok_a)

    tok_b = set_cwd_scope("agent-b")
    try:
        # Agent B never cd'd — it must start at the root, not in A's subdir.
        assert get_cwd(session_id, str(root)) == str(root)
    finally:
        reset_cwd_scope(tok_b)


def test_scopes_survive_real_concurrent_tasks(tmp_path):
    session_id = "wf-session-gather"
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()

    async def _agent(scope: str, target: str, delay: float):
        tok = set_cwd_scope(scope)
        try:
            update_cwd(session_id, target)
            await asyncio.sleep(delay)  # let the sibling interleave
            return get_cwd(session_id, "/default")
        finally:
            reset_cwd_scope(tok)
            clear_scope(session_id, scope)

    async def _run():
        return await asyncio.gather(
            _agent("agent-a", str(a_dir), 0.05),
            _agent("agent-b", str(b_dir), 0.0),
        )

    got_a, got_b = asyncio.run(_run())
    assert got_a == str(a_dir)
    assert got_b == str(b_dir)


def test_no_scope_keeps_the_bare_session_key(tmp_path):
    """Ordinary interactive chat sets no scope — its key must stay exactly
    the plain session_id it always was."""
    target = tmp_path / "x"
    target.mkdir()
    update_cwd("plain-session", str(target))
    assert list(_session_cwd) == ["plain-session"]
    assert get_cwd("plain-session", "/default") == str(target)


def test_scope_and_worktree_override_compose(tmp_path):
    from server.workspace.state import reset_workspace_override, set_workspace_override

    session_id = "wf-both"
    wt = tmp_path / "worktree-agent-a"
    (wt / "deep").mkdir(parents=True)

    ws_tok = set_workspace_override(str(wt))
    register_override(str(wt))
    scope_tok = set_cwd_scope("agent-a")
    try:
        update_cwd(session_id, str(wt / "deep"))
        assert get_cwd(session_id, str(wt)) == str(wt / "deep")
    finally:
        reset_cwd_scope(scope_tok)
        reset_workspace_override(ws_tok)

    # A different agent, same worktree path, must still be isolated.
    ws_tok2 = set_workspace_override(str(wt))
    scope_tok2 = set_cwd_scope("agent-b")
    try:
        assert get_cwd(session_id, str(wt)) == str(wt)
    finally:
        reset_cwd_scope(scope_tok2)
        reset_workspace_override(ws_tok2)


def test_clear_scope_removes_only_that_agents_entries(tmp_path):
    session_id = "wf-clear"
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    for scope, target in (("agent-a", a), ("agent-b", b)):
        tok = set_cwd_scope(scope)
        try:
            update_cwd(session_id, str(target))
        finally:
            reset_cwd_scope(tok)

    clear_scope(session_id, "agent-a")

    tok = set_cwd_scope("agent-b")
    try:
        assert get_cwd(session_id, "/default") == str(b)  # B survived
    finally:
        reset_cwd_scope(tok)
    tok = set_cwd_scope("agent-a")
    try:
        assert get_cwd(session_id, "/default") == "/default"  # A is gone
    finally:
        reset_cwd_scope(tok)


def test_run_agent_sets_and_clears_its_own_cwd_scope():
    import inspect

    from server.agents import runtime

    src = inspect.getsource(runtime.run_agent)
    assert "set_cwd_scope(agent_id)" in src
    assert "clear_scope(session_id, agent_id)" in src


# ── 1. stop vs natural finish: first writer wins ────────────────────────────


class _StopRaceBase:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
        os.makedirs(tmp_path / "data", exist_ok=True)
        from server.infrastructure import sessions

        storage = tmp_path / "storage"
        os.makedirs(storage, exist_ok=True)
        monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
        monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
        from server.tasks import registry

        monkeypatch.setattr(registry, "STORAGE_DIR", str(storage))
        monkeypatch.setattr(registry, "DB_PATH", str(storage / "sessions.db"))
        from server.workflows import manager

        manager._live.clear()
        manager._task_ids.clear()
        yield
        manager._live.clear()
        manager._task_ids.clear()


class TestStopRace(_StopRaceBase):
    def _seed_running_row(self, run_id="r-race"):
        from server.workflows import manager

        with manager._conn() as conn:
            manager._ensure_table(conn)
            conn.execute(
                "INSERT INTO workflow_runs (run_id, name, session_id, status, started_at) "
                "VALUES (?,?,?,?,?)",
                (run_id, "race", "s1", "running", manager._now()),
            )
        return run_id

    def test_a_stopped_run_is_not_relabelled_failed(self):
        """The race: stop_run writes 'stopped', then the cancelled harness's
        own outcome ('failed') lands and used to overwrite it."""
        from server.workflows import manager
        from server.workflows.runtime import WorkflowRun

        run_id = self._seed_running_row()
        with manager._conn() as conn:
            conn.execute("UPDATE workflow_runs SET status='stopped' WHERE run_id=?", (run_id,))

        run = WorkflowRun(run_id, "", session_id="s1")
        manager._finalize(run, {"status": "failed", "error": "harness killed"})

        assert manager.get_run(run_id)["status"] == "stopped"

    def test_a_naturally_finished_run_still_records_its_outcome(self):
        from server.workflows import manager
        from server.workflows.runtime import WorkflowRun

        run_id = self._seed_running_row("r-normal")
        run = WorkflowRun(run_id, "", session_id="s1")
        manager._finalize(run, {"status": "done", "result": {"ok": 1}, "cost_usd": 1.25})

        row = manager.get_run(run_id)
        assert row["status"] == "done"
        assert row["cost_usd"] == 1.25

    def test_finalize_announces_the_end_only_once(self, monkeypatch):
        from server.workflows import manager
        from server.workflows.runtime import WorkflowRun

        published = []
        monkeypatch.setattr(manager, "_publish", lambda *a, **k: published.append(a))

        run_id = self._seed_running_row("r-once")
        run = WorkflowRun(run_id, "", session_id="s1")
        manager._finalize(run, {"status": "done"})
        manager._finalize(run, {"status": "failed"})  # a second, losing writer

        assert len(published) == 1
        assert manager.get_run(run_id)["status"] == "done"


# ── 3. static tool catalogs: a deliberate trade-off, pinned by test ─────────


def test_workspace_tools_fail_safely_when_the_workspace_vanishes(monkeypatch):
    """An agent's tool catalog is built once, so connecting or disconnecting
    a workspace mid-run does not rebuild it. Rebuilding per round would
    invalidate the prompt-cache prefix the whole engine is designed around,
    so the catalog stays static — which is safe ONLY because the tools
    themselves re-check the workspace at execution time and refuse cleanly
    rather than acting on a stale root. This pins that refusal."""
    from server.approval.bootstrap import _do_command

    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    outcome = asyncio.run(_do_command({"command": "ls"}))
    assert outcome.ok is False
    assert "no workspace" in (outcome.error or "").lower()

    from server.workspace.executors import _exec_ws_run_command

    monkeypatch.setattr("server.workspace.executors.get_workspace_path", lambda: None)
    out = _exec_ws_run_command({"command": "ls"}, transcript=[], current_attachments=[])
    assert "no workspace" in out.lower()
