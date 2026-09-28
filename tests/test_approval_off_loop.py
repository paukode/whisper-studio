"""Approved shell, git and python commands run off the event loop.

Every approval executor is awaited on the single uvicorn loop, both by the
approval card (POST /api/approval/execute) and by the in-turn auto-approve
path. A synchronous body there froze every session (streams, Stop, new turns)
for as long as the command ran. The contract: while an approved action runs,
the loop keeps serving other coroutines.
"""

import asyncio
import time

import pytest


async def _ticks_while(coro, *, interval: float = 0.05) -> tuple[object, int]:
    """Run ``coro`` beside a ticker; return its result and the tick count."""
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(interval)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        done.set()
        await t
    return result, ticks


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path))
    return tmp_path


def test_approved_command_does_not_block_the_loop(workspace_dir):
    from server.approval.executors import _do_command

    outcome, ticks = asyncio.run(_ticks_while(_do_command({"command": "sleep 0.6"})))
    assert outcome.ok, outcome.error
    # 0.6 s at a 50 ms cadence: a blocked loop ticks at most once, at the end.
    assert ticks >= 5, f"the loop was frozen while the approved command ran ({ticks} ticks)"


def test_inline_auto_approved_command_does_not_block_the_loop(workspace_dir):
    """The in-turn path ("Yes, all cli", auto mode, subagents) awaits the same
    executor through _execute_ws_approval_inline."""
    from server.approval.bootstrap import register_defaults
    from server.tool_executor import _execute_ws_approval_inline

    register_defaults()
    result, ticks = asyncio.run(
        _ticks_while(_execute_ws_approval_inline({"action": "command", "command": "sleep 0.6"}))
    )
    assert not str(result).startswith("Error"), result
    assert ticks >= 5, f"the loop was frozen by the inline approval ({ticks} ticks)"


@pytest.mark.parametrize(
    ("executor_name", "module", "sync_name"),
    [
        ("_do_git_push", "server.git.executor", "do_git_push"),
        ("_do_git_clone", "server.git.executor", "do_git_clone"),
        ("_do_github", "server.git.gh_executor", "do_github"),
        ("_do_run_python", "server.executors.code", "do_run_python"),
        ("_do_aws_cli", "server.executors.code", "do_aws_cli"),
    ],
)
def test_sync_bodied_executors_run_off_the_loop(monkeypatch, executor_name, module, sync_name):
    from server.approval import executors

    def slow(payload):
        time.sleep(0.5)
        return True, "done"

    monkeypatch.setattr(f"{module}.{sync_name}", slow)
    fn = getattr(executors, executor_name)
    outcome, ticks = asyncio.run(_ticks_while(fn({})))
    assert outcome.ok and outcome.output == "done"
    assert ticks >= 4, f"{executor_name} froze the loop ({ticks} ticks)"


@pytest.mark.parametrize(
    ("executor_name", "sync_name", "payload"),
    [
        ("_do_enter_worktree", "enter_worktree", {"name": "fix-a", "session_id": "S"}),
        ("_do_exit_worktree", "exit_worktree", {"session_id": "S"}),
    ],
)
def test_worktree_executors_run_off_the_loop(
    workspace_dir, monkeypatch, executor_name, sync_name, payload
):
    from types import SimpleNamespace

    from server.approval import executors

    def slow(*args, **kwargs):
        time.sleep(0.5)
        return SimpleNamespace(
            worktree_name="fix-a",
            worktree_path="/w",
            worktree_branch="worktree-fix-a",
            original_cwd="/r",
        )

    monkeypatch.setattr(f"server.git.worktree_session.{sync_name}", slow)
    fn = getattr(executors, executor_name)
    outcome, ticks = asyncio.run(_ticks_while(fn(payload)))
    assert outcome.ok, outcome.error
    assert ticks >= 4, f"{executor_name} froze the loop ({ticks} ticks)"


class _JsonRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self) -> dict:
        return self._body


def test_workspace_shell_endpoint_does_not_block_the_loop(tmp_path, monkeypatch):
    from server.workspace.routes import shell as shell_routes

    monkeypatch.setattr(shell_routes, "get_workspace_path", lambda: str(tmp_path))
    req = _JsonRequest({"command": "sleep 0.6"})
    resp, ticks = asyncio.run(_ticks_while(shell_routes.ws_shell_endpoint(req)))
    assert resp["exit_code"] == 0, resp
    assert ticks >= 5, f"/api/workspace/shell froze the loop ({ticks} ticks)"


def test_approved_delete_of_a_large_tree_does_not_block_the_loop(workspace_dir, monkeypatch):
    from server.approval import executors

    tree = workspace_dir / "node_modules"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "index.js").write_text("x")
    real_rmtree = executors.shutil.rmtree

    def slow_rmtree(path, *a, **kw):
        time.sleep(0.5)
        real_rmtree(path, *a, **kw)

    monkeypatch.setattr(executors.shutil, "rmtree", slow_rmtree)
    outcome, ticks = asyncio.run(_ticks_while(executors._do_delete({"path": "node_modules"})))
    assert outcome.ok, outcome.error
    assert not tree.exists()
    assert ticks >= 4, f"the tree delete froze the loop ({ticks} ticks)"


def test_approved_delete_of_a_file_keeps_its_text_for_undo(workspace_dir):
    from server import workspace
    from server.approval import executors

    (workspace_dir / "notes.txt").write_text("keep me")
    outcome = asyncio.run(executors._do_delete({"path": "notes.txt"}))
    assert outcome.ok, outcome.error
    assert not (workspace_dir / "notes.txt").exists()
    assert workspace.WORKSPACE_BACKUPS.pop("notes.txt") == "keep me"
