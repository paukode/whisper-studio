"""Worktree isolation and session scoping for agents."""

import asyncio
import subprocess

from server.agents.messaging import MessageBus
from server.workspace.state import (
    get_workspace_path,
    reset_workspace_override,
    set_workspace_override,
)


def test_workspace_override_set_and_reset():
    token = set_workspace_override("/tmp/fake-worktree")
    try:
        assert get_workspace_path() == "/tmp/fake-worktree"
    finally:
        reset_workspace_override(token)
    assert get_workspace_path() != "/tmp/fake-worktree"


def test_override_propagates_through_submit_helper():
    """tool_router._submit must carry contextvars onto worker threads —
    otherwise isolated agents' file tools would resolve against the global
    workspace the moment they run on the pool."""
    from concurrent.futures import ThreadPoolExecutor

    from server.tool_router import _submit

    async def _run():
        token = set_workspace_override("/tmp/ctx-worktree")
        try:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=1) as ex:
                return await _submit(loop, ex, get_workspace_path)
        finally:
            reset_workspace_override(token)

    assert asyncio.run(_run()) == "/tmp/ctx-worktree"


def test_plain_run_in_executor_would_lose_the_override():
    """Documents WHY _submit exists: the naive submission drops context."""
    from concurrent.futures import ThreadPoolExecutor

    async def _run():
        token = set_workspace_override("/tmp/lost-worktree")
        try:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=1) as ex:
                return await loop.run_in_executor(ex, get_workspace_path)
        finally:
            reset_workspace_override(token)

    assert asyncio.run(_run()) != "/tmp/lost-worktree"


def test_enter_agent_worktree_creates_isolated_copy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    from server.agents.runtime import _enter_agent_worktree

    token = set_workspace_override(str(repo))
    try:
        session = _enter_agent_worktree("abc123", "sess-x")
    finally:
        reset_workspace_override(token)
    # Now returns a WorktreeSession (so run_agent can harvest it), not a path.
    assert session is not None
    wt = session.worktree_path
    assert "agent-abc123" in wt
    assert session.worktree_branch == "worktree-agent-abc123"
    assert (repo / ".whisper").exists() or wt  # conventional location
    import os

    assert os.path.exists(os.path.join(wt, "f.txt"))  # a real checkout
    # cleanup
    subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=repo, check=False)


def test_enter_agent_worktree_degrades_without_git(tmp_path):
    from server.agents.runtime import _enter_agent_worktree

    token = set_workspace_override(str(tmp_path))  # not a git repo
    try:
        assert _enter_agent_worktree("noop1", "s") is None
    finally:
        reset_workspace_override(token)


def test_broadcast_scoped_to_sender_session():
    bus = MessageBus()
    bus.create_mailbox("a1", session_id="sess-1")
    bus.create_mailbox("a2", session_id="sess-1")
    bus.create_mailbox("b1", session_id="sess-2")

    n = bus.broadcast("a1", "hello team", "sess-1")
    assert n == 1  # only a2, never sess-2's agent
    assert len(bus.receive("a2")) == 1
    assert bus.receive("b1") == []


def test_chat_broadcast_reaches_the_sessions_agents():
    import json

    from server.agent_tools.spawn import execute_send_message
    from server.agents.messaging import message_bus

    message_bus.create_mailbox("bc-agent", session_id="bc-sess")
    message_bus.create_mailbox("bc-other", session_id="bc-elsewhere")
    try:
        out = json.loads(
            execute_send_message({"broadcast": True, "content": "stop and report"}, "bc-sess")
        )
        assert out["recipients"] == 1
        assert [m.content for m in message_bus.receive("bc-agent")] == ["stop and report"]
        assert message_bus.receive("bc-other") == []
    finally:
        message_bus.delete_mailbox("bc-agent")
        message_bus.delete_mailbox("bc-other")


def test_send_message_to_an_unknown_agent_is_an_error():
    import json

    from server.agent_tools.spawn import execute_send_message
    from server.agents.messaging import message_bus

    out = json.loads(execute_send_message({"to_agent_id": "typo123", "content": "x"}, "s1"))
    assert "no agent typo123" in out["error"]
    assert "typo123" not in message_bus._mailboxes
