"""Report item #4: a worktree-isolated agent must never resolve its shell
command's cwd against the parent session's stored cwd — that stored path
points at the ORIGINAL checkout, not the isolated worktree.

Reproduction from the report: connect repo A, run a command that leaves the
session's stored cwd at A/subdir, then start a worktree-isolated agent and run
a command in it. Before the fix, get_cwd(session_id, ws) returned A/subdir
(since it exists on disk) instead of the worktree root.
"""

from __future__ import annotations

import os

from server.cwd_tracker import clear_override, get_cwd, update_cwd
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


def test_a_cwd_that_escaped_the_worktree_is_not_trusted(tmp_path):
    """Defense in depth: even if some other bug stored a cwd outside the
    override, get_cwd must not hand it back."""
    worktree = tmp_path / "worktree-agent-ghi"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    session_id = "sess-shared-4"

    # Simulate a stored cwd from before isolation was scoped (or any future
    # bug that writes outside the override) by writing directly to the
    # unscoped key update_cwd would have used pre-fix.
    import server.cwd_tracker as ct

    with ct._lock:
        ct._session_cwd[session_id] = str(outside)

    token = set_workspace_override(str(worktree))
    try:
        assert get_cwd(session_id, str(worktree)) == str(worktree)
    finally:
        reset_workspace_override(token)
        with ct._lock:
            ct._session_cwd.pop(session_id, None)


def test_update_cwd_refuses_to_store_a_path_outside_the_override(tmp_path):
    worktree = tmp_path / "worktree-agent-jkl"
    worktree.mkdir()
    outside = tmp_path / "outside2"
    outside.mkdir()
    session_id = "sess-shared-5"

    token = set_workspace_override(str(worktree))
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
    try:
        update_cwd(session_id, str(wt_a))
    finally:
        reset_workspace_override(token_a)

    token_b = set_workspace_override(str(wt_b))
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


def test_run_agent_cleans_up_its_cwd_entry_on_exit(tmp_path):
    """The finally block in run_agent must clear the per-worktree cwd slot,
    or every isolated run ever made leaks one entry for the life of the
    process."""
    import inspect

    from server.agents import runtime

    src = inspect.getsource(runtime.run_agent)
    assert "clear_override" in src, "run_agent no longer cleans up its cwd entry"
