"""Report item #4: a worktree-isolated agent must never resolve its shell
command's cwd against the parent session's stored cwd — that stored path
points at the ORIGINAL checkout, not the isolated worktree.

Reproduction from the report: connect repo A, run a command that leaves the
session's stored cwd at A/subdir, then start a worktree-isolated agent and run
a command in it. Before the fix, get_cwd(session_id, ws) returned A/subdir
(since it exists on disk) instead of the worktree root.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from unittest.mock import AsyncMock

from server.cwd_tracker import clear_override, get_cwd, register_override, update_cwd
from server.workspace.state import reset_workspace_override, set_workspace_override


def test_isolated_agent_ignores_the_parents_stale_cwd(tmp_path):
    original_checkout = tmp_path / "repo"
    subdir = original_checkout / "src"
    subdir.mkdir(parents=True)
    worktree = tmp_path / "worktree-agent-abc"
    worktree.mkdir()

    session_id = "sess-shared"
    # The parent session ran a command in repo/src before the agent started —
    # exactly the report's reproduction step.
    update_cwd(session_id, str(subdir))

    token = set_workspace_override(str(worktree))
    try:
        effective = get_cwd(session_id, str(worktree))
        assert effective == str(worktree), (
            f"isolated agent resolved cwd to {effective!r}, "
            f"which is inside the ORIGINAL checkout, not its worktree"
        )
    finally:
        reset_workspace_override(token)
        clear_override(session_id, str(worktree))

    # And the parent session's own cwd is untouched.
    assert get_cwd(session_id, "/default") == str(subdir)


def test_isolated_agent_cwd_does_not_leak_into_the_parent_session(tmp_path):
    original_checkout = tmp_path / "repo2"
    original_checkout.mkdir()
    worktree = tmp_path / "worktree-agent-def"
    (worktree / "nested").mkdir(parents=True)

    session_id = "sess-shared-2"
    token = set_workspace_override(str(worktree))
    register_override(str(worktree))
    try:
        # The isolated agent cd's into a subdirectory of ITS worktree.
        update_cwd(session_id, str(worktree / "nested"))
    finally:
        reset_workspace_override(token)
        clear_override(session_id, str(worktree))

    # Back in the parent's (non-isolated) context, its cwd must still be the
    # default — the agent's cd never touched the parent's slot.
    assert get_cwd(session_id, str(original_checkout)) == str(original_checkout)


def test_two_concurrent_isolated_agents_do_not_share_a_cwd_slot(tmp_path):
    session_id = "sess-shared-3"  # a workflow's agents share one session_id
    wt_a = tmp_path / "worktree-agent-a"
    wt_b = tmp_path / "worktree-agent-b"
    (wt_a / "dir-a").mkdir(parents=True)
    (wt_b / "dir-b").mkdir(parents=True)

    token_a = set_workspace_override(str(wt_a))
    register_override(str(wt_a))
    try:
        update_cwd(session_id, str(wt_a / "dir-a"))
    finally:
        reset_workspace_override(token_a)

    token_b = set_workspace_override(str(wt_b))
    try:
        # Agent B never cd'd anywhere — must get ITS worktree root, not A's
        # stored subdirectory.
        assert get_cwd(session_id, str(wt_b)) == str(wt_b)
    finally:
        reset_workspace_override(token_b)
        clear_override(session_id, str(wt_a))
        clear_override(session_id, str(wt_b))


def test_two_concurrent_isolated_agents_via_real_asyncio_concurrency(tmp_path):
    """The sequential version above proves key-scoping; this proves the
    contextvar itself stays per-Task under GENUINE concurrent execution — two
    coroutines actually interleaved via asyncio.gather, the way a workflow's
    sibling agents really run."""
    session_id = "sess-shared-gather"
    wt_a = tmp_path / "worktree-agent-gather-a"
    wt_b = tmp_path / "worktree-agent-gather-b"
    wt_a.mkdir()
    wt_b.mkdir()

    async def _agent(worktree: str, delay_before_read: float):
        token = set_workspace_override(worktree)
        register_override(worktree)
        try:
            update_cwd(session_id, worktree)
            await asyncio.sleep(delay_before_read)  # let the other task interleave
            return get_cwd(session_id, worktree)
        finally:
            reset_workspace_override(token)
            clear_override(session_id, worktree)

    async def _run():
        # Staggered delays force real interleaving: A is still "awake" inside
        # its own await when B sets, writes, and clears its own override.
        return await asyncio.gather(_agent(str(wt_a), 0.05), _agent(str(wt_b), 0.0))

    result_a, result_b = asyncio.run(_run())
    assert result_a == str(wt_a)
    assert result_b == str(wt_b)


def test_a_cwd_that_escaped_the_worktree_is_not_trusted(tmp_path):
    """Defense in depth: even if some other bug stored a cwd outside the
    override UNDER THE CORRECTLY SCOPED KEY, get_cwd must not hand it back."""
    worktree = tmp_path / "worktree-agent-ghi"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    session_id = "sess-shared-4"

    import server.cwd_tracker as ct

    scoped_key = ct._key(session_id, str(worktree))
    with ct._lock:
        ct._session_cwd[scoped_key] = str(outside)

    token = set_workspace_override(str(worktree))
    try:
        assert get_cwd(session_id, str(worktree)) == str(worktree)
    finally:
        reset_workspace_override(token)
        with ct._lock:
            ct._session_cwd.pop(scoped_key, None)


def test_update_cwd_refuses_to_store_a_path_outside_the_override(tmp_path):
    worktree = tmp_path / "worktree-agent-jkl"
    worktree.mkdir()
    outside = tmp_path / "outside2"
    outside.mkdir()
    session_id = "sess-shared-5"

    token = set_workspace_override(str(worktree))
    register_override(str(worktree))
    try:
        update_cwd(session_id, str(outside))  # e.g. `cd /somewhere/else`
        assert get_cwd(session_id, str(worktree)) == str(worktree)
    finally:
        reset_workspace_override(token)
        clear_override(session_id, str(worktree))


def test_clear_override_removes_only_that_worktrees_entry(tmp_path):
    wt_a = tmp_path / "wt-a"
    wt_b = tmp_path / "wt-b"
    wt_a.mkdir()
    wt_b.mkdir()
    session_id = "sess-shared-6"

    token_a = set_workspace_override(str(wt_a))
    register_override(str(wt_a))
    try:
        update_cwd(session_id, str(wt_a))
    finally:
        reset_workspace_override(token_a)

    token_b = set_workspace_override(str(wt_b))
    register_override(str(wt_b))
    try:
        update_cwd(session_id, str(wt_b))
    finally:
        reset_workspace_override(token_b)

    clear_override(session_id, str(wt_a))

    token_b2 = set_workspace_override(str(wt_b))
    try:
        # B's entry survived A's teardown.
        assert get_cwd(session_id, "/default") == str(wt_b)
    finally:
        reset_workspace_override(token_b2)
        clear_override(session_id, str(wt_b))


def test_update_cwd_refuses_a_write_for_an_unregistered_override(tmp_path):
    """register_override is the contract every real caller (run_agent,
    run_headless_turn) follows. An override nobody registered is treated the
    same as one already retired — refused, not silently trusted — since the
    only way to reach this state in production is exactly the bug being
    guarded against."""
    worktree = tmp_path / "worktree-agent-unreg"
    worktree.mkdir()
    session_id = "sess-unregistered"

    token = set_workspace_override(str(worktree))
    try:
        update_cwd(session_id, str(worktree))  # no register_override call
        assert get_cwd(session_id, "/default") == "/default"
    finally:
        reset_workspace_override(token)


def test_a_write_after_clear_override_is_refused_not_resurrected(tmp_path):
    """The exact race the adversarial review found: an isolated agent is
    cancelled while a shell command is still running in a worker thread.
    run_agent's finally retires the override immediately; the abandoned
    thread's update_cwd call — dispatched before cancellation, so it still
    carries the override in its own frozen context — must not bring the
    entry back, since nothing will ever clear it again once the agent that
    owned it is gone."""
    worktree = tmp_path / "worktree-agent-race"
    worktree.mkdir()
    session_id = "sess-race"

    token = set_workspace_override(str(worktree))
    register_override(str(worktree))
    # Freeze a copy of the context WHILE the override is active — exactly what
    # tool_router._submit does when it dispatches a tool call onto a worker
    # thread (a bare threading.Thread does NOT inherit contextvars on its own,
    # so without this the "abandoned" write below would run with no override
    # at all and this test would prove nothing).
    import contextvars

    ctx = contextvars.copy_context()

    released = threading.Event()

    def _delayed_write():
        released.wait(timeout=2)
        # Runs the SAME code a worker thread executing ws_run_command would,
        # with the override still visible in its own frozen context.
        ctx.run(update_cwd, session_id, str(worktree))

    worker = threading.Thread(target=_delayed_write)
    worker.start()
    try:
        # Simulate run_agent's finally firing immediately on cancellation,
        # BEFORE the abandoned worker thread's command finishes.
        reset_workspace_override(token)
        clear_override(session_id, str(worktree))
    finally:
        released.set()
        worker.join(timeout=2)

    # Re-enter the override to check its slot the way a fresh lookup would.
    token2 = set_workspace_override(str(worktree))
    try:
        assert get_cwd(session_id, "/default") == "/default", (
            "a write that outlived its agent resurrected a retired cwd entry"
        )
    finally:
        reset_workspace_override(token2)


def test_do_command_resolves_cwd_inside_the_override(tmp_path):
    """The SECOND real call site: an agent's auto-approved write command
    (server/approval/bootstrap.py::_do_command) must resolve cwd through the
    same override-aware path as the direct-execution one, not the parent
    session's stale cwd."""
    from server.approval.bootstrap import _do_command

    original_checkout = tmp_path / "repo3"
    subdir = original_checkout / "src"
    subdir.mkdir(parents=True)
    worktree = tmp_path / "worktree-agent-mno"
    worktree.mkdir()

    session_id = "sess-do-command"
    update_cwd(session_id, str(subdir))  # parent's stale cwd, same as the repro

    token = set_workspace_override(str(worktree))
    register_override(str(worktree))
    try:
        outcome = asyncio.run(_do_command({"command": "pwd", "session_id": session_id}))
    finally:
        reset_workspace_override(token)
        clear_override(session_id, str(worktree))

    assert outcome.ok, outcome.error
    assert outcome.output.strip() == str(worktree)


def _fake_git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_run_agent_registers_and_clears_its_override_on_success(monkeypatch, tmp_path):
    """A real run_agent(isolation='worktree') call: real worktree creation,
    real override register/clear, only the turn loop itself mocked out —
    replacing a test that only grepped run_agent's source text for the
    string 'clear_override' and never actually ran it."""
    from server.agents.runtime import AgentResult, run_agent

    repo = _fake_git_repo(tmp_path)
    # _enter_agent_worktree runs via loop.run_in_executor, which does NOT
    # propagate contextvars to the worker thread — a real agent isolates from
    # the GLOBALLY connected workspace (the on-disk config), not a caller's
    # own contextvar override, and no override is set yet at this point in
    # run_agent anyway. Point the global workspace at the repo directly.
    monkeypatch.setattr("server.workspace.state.load_workspace_config", lambda: {"path": str(repo)})
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

    result = asyncio.run(
        run_agent(
            "task",
            agent_type="general",
            session_id="sess-real-1",
            model_id_override="test-model",
            isolation="worktree",
        )
    )

    assert result.status == "completed"
    assert len(calls) == 2
    assert calls[0][0] == "register"
    assert calls[1] == ("clear", "sess-real-1", calls[0][1])
    assert "worktree" in calls[0][1] or "agent-" in calls[0][1]


def test_run_agent_clears_its_override_even_when_the_loop_raises(monkeypatch, tmp_path):
    from server.agents.runtime import run_agent

    repo = _fake_git_repo(tmp_path)
    monkeypatch.setattr("server.workspace.state.load_workspace_config", lambda: {"path": str(repo)})
    calls: list[tuple] = []
    monkeypatch.setattr(
        "server.cwd_tracker.register_override", lambda o: calls.append(("register", o))
    )
    monkeypatch.setattr(
        "server.cwd_tracker.clear_override", lambda s, o: calls.append(("clear", s, o))
    )
    monkeypatch.setattr(
        "server.agents.runtime._run_agent_loop",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    result = asyncio.run(
        run_agent(
            "task",
            agent_type="general",
            session_id="sess-real-2",
            model_id_override="test-model",
            isolation="worktree",
        )
    )

    # run_agent's own except Exception branch swallows this into a failed
    # result rather than propagating — cleanup must still have run.
    assert result.status == "failed"
    assert [c[0] for c in calls] == ["register", "clear"]


def test_headless_turn_registers_and_clears_its_cwd_override(monkeypatch, tmp_path):
    """run_headless_turn's own workspace override (a DIFFERENT call site from
    run_agent's, with no worktree teardown to piggyback cleanup on) must
    follow the same register/clear contract, or every call that passes
    workspace_path leaks a cwd_tracker entry — not merely on a cancellation
    race, but on every single call."""
    from server.exec.headless import run_headless_turn
    from server.workspace.state import get_workspace_path as real_get_workspace_path
    from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block
    from tests.test_headless_turn import _collect, _patch_common

    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("done"), *msg_end(stop_reason="end_turn")]]
    )
    _patch_common(monkeypatch, fake_stream)
    # _patch_common stubs get_workspace_path to always return None; restore the
    # real one so this test's own workspace_path override is actually visible.
    monkeypatch.setattr("server.workspace.get_workspace_path", real_get_workspace_path)

    calls: list[tuple] = []
    monkeypatch.setattr(
        "server.cwd_tracker.register_override", lambda o: calls.append(("register", o))
    )
    monkeypatch.setattr(
        "server.cwd_tracker.clear_override", lambda s, o: calls.append(("clear", s, o))
    )

    ws = str(tmp_path)
    events = asyncio.run(
        _collect(
            run_headless_turn(
                "hi", workspace_path=ws, ephemeral=True, session_id="sess-headless-cwd"
            )
        )
    )
    assert events[-1]["status"] == "completed"
    assert calls == [("register", ws), ("clear", "sess-headless-cwd", ws)]
