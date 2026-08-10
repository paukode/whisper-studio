"""Phase 1: server/agents/runtime.py's inner loop ported onto the shared
chat/engine turn loop (server.chat.engine.runner.run_turn).

These tests exercise the REAL run_agent()/_run_agent_loop() path end to end
(no mocking of run_agent or _run_agent_loop itself) — a scripted streaming
Bedrock client (tests/golden_harness.FakeBedrockClient, the same harness
interactive chat's golden fixtures use) stands in for the model, since the
agent loop's main model call now goes through the exact same
server/chat/engine/anthropic.AnthropicAdapter interactive chat uses.
"""

import asyncio
import subprocess

from server.agents.config import AgentConfig
from server.agents.runtime import run_agent
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


# ── __agent__ stamp reaches route_tool through the new run_turn-backed path ─


def test_agent_stamp_reaches_route_tool_through_new_loop(monkeypatch):
    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_grep", {"pattern": "x"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Found it."), *msg_end(stop_reason="end_turn")],
        ]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    captured: dict = {}

    async def fake_route_tool(tool_name, tool_input, **kw):
        captured["tool_input"] = dict(tool_input)
        captured["session_id"] = kw.get("session_id")
        return "matches found", []

    monkeypatch.setattr("server.tool_executor.route_tool", fake_route_tool)

    result = asyncio.run(
        run_agent(
            "search the repo",
            agent_type="general",
            session_id="sess-stamp-check",
            model_id_override="test-model",
        )
    )

    # The riskiest failure mode of this whole migration: silently losing the
    # __agent__ stamp on the new tool-dispatch path. It must reach route_tool.
    assert captured["tool_input"]["__agent__"] is True
    assert captured["tool_input"]["__session_id__"] == "sess-stamp-check"
    assert captured["session_id"] == "sess-stamp-check"
    assert result.status == "completed"
    assert "Found it." in result.output


# ── a subagent does not perturb the parent session's ACTIVE goal state ─────


def test_spawned_agent_does_not_perturb_parent_session_goal_state(monkeypatch, tmp_path):
    """Proves the turn_scope_id fix against the REAL spawn path (run_agent),
    not just the synthetic Phase 0 unit test: a subagent spawned from a
    session with an active goal must not perturb that goal's consecutive-
    block counter, since execute_spawn_agent already passes the PARENT
    session's own id as session_id."""
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    from server.infrastructure import sessions

    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
    monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
    sessions._ensure_db()
    with sessions._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("parent-sess-goal", "t", "2026-01-01", "2026-01-01"),
        )

    from server.goals import store as goal_store

    goal_store.set_goal("parent-sess-goal", "Ship the feature", set_at="t0")
    goal_store.record_block("parent-sess-goal", "not_achieved", "keep going")
    goal_store.record_block("parent-sess-goal", "not_achieved", "keep going")
    before = goal_store.get_goal("parent-sess-goal")["state"]["consecutive_blocks"]
    assert before == 2

    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("Subtask done."), *msg_end(stop_reason="end_turn")]]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    # session_id is the PARENT chat session's own id, exactly as
    # execute_spawn_agent/spawn_agent already thread it through.
    result = asyncio.run(
        run_agent(
            "handle a subtask",
            agent_type="general",
            session_id="parent-sess-goal",
            model_id_override="test-model",
        )
    )
    assert result.status == "completed"

    after = goal_store.get_goal("parent-sess-goal")["state"]["consecutive_blocks"]
    assert after == before == 2, (
        "the subagent's inner turn must not touch the parent session's own "
        "goal-tracking state (turn_scope_id must key it, not the raw session_id)"
    )


# ── read_only agent config still cannot reach write tools via tool_catalog ──


def test_read_only_agent_cannot_reach_write_tools_via_new_tool_catalog(monkeypatch):
    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("Looked around."), *msg_end(stop_reason="end_turn")]]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    _mock_tools = [
        {"name": "ws_read_file", "description": "d", "input_schema": {"type": "object"}},
        {"name": "ws_write_file", "description": "d", "input_schema": {"type": "object"}},
    ]
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool",
        lambda *a, **k: (_mock_tools, [], len(_mock_tools)),
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    cfg = AgentConfig(agent_type="explore", read_only=True, max_turns=5, deadline_seconds=None)
    result = asyncio.run(
        run_agent("look around", config=cfg, session_id="", model_id_override="test-model")
    )

    assert result.status == "completed"
    # The real wire request (TurnContext.tool_catalog -> filter_tools_for_agent)
    # must never have offered the write tool to the model.
    sent_tool_names = {t["name"] for t in fake_stream.requests[0].get("tools", [])}
    assert "ws_write_file" not in sent_tool_names
    assert "ws_read_file" in sent_tool_names


# ── worktree isolation for a detached agent still works end-to-end ─────────


def test_worktree_isolation_end_to_end_through_new_loop(monkeypatch, tmp_path):
    """A full round trip through the NEW inner loop: enter an isolated
    worktree, auto-approve a real ws_create_file write inside it (Phase 0's
    unattended WS_APPROVAL handling), finish naturally, and harvest the
    change back onto the originating repo — proving the outer run_agent()
    shell (worktree enter/harvest) still works unmodified with the new loop
    underneath it."""
    from server.approval.bootstrap import register_defaults

    register_defaults()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], str(repo))
    _git(["config", "user.email", "t@t"], str(repo))
    _git(["config", "user.name", "t"], str(repo))
    (repo / "base.txt").write_text("base\n")
    (repo / ".gitignore").write_text(".whisper/\n")
    _git(["add", "-A"], str(repo))
    _git(["commit", "-m", "base"], str(repo))

    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(repo))

    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("c1", "ws_create_file", {"path": "new.txt", "content": "hi\n"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Created new.txt."), *msg_end(stop_reason="end_turn")],
        ]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )

    cfg = AgentConfig(agent_type="general", read_only=False, max_turns=5, deadline_seconds=None)
    result = asyncio.run(
        run_agent(
            "create new.txt",
            config=cfg,
            session_id="",
            model_id_override="test-model",
            isolation="worktree",
        )
    )

    assert result.status == "completed"
    assert result.stopped_early is False
    assert "Created new.txt." in result.output
    # The harvest note (or the absence of a problem) reaches result.output —
    # and the file genuinely landed back on the originating repo, uncommitted.
    assert (repo / "new.txt").exists()
    assert (repo / "new.txt").read_text() == "hi\n"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert "new.txt" in status, "harvested change must be uncommitted (git status shows it)"


def test_worktree_kept_not_applied_on_turn_limit_exit(monkeypatch, tmp_path):
    """The stopped_early counterpart: a turn-limited run must NOT apply its
    worktree changes — result.status/result.stopped_early must reflect the
    capped exit so the outer shell keeps the worktree for inspection."""
    from server.approval.bootstrap import register_defaults

    register_defaults()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], str(repo))
    _git(["config", "user.email", "t@t"], str(repo))
    _git(["config", "user.name", "t"], str(repo))
    (repo / "base.txt").write_text("base\n")
    (repo / ".gitignore").write_text(".whisper/\n")
    _git(["add", "-A"], str(repo))
    _git(["commit", "-m", "base"], str(repo))

    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(repo))

    # Round 0: a real tool call (tools are genuinely available). Round 1 is
    # max_turns=2's forced-no-tools last round — a real model has nothing
    # left to call, so it just answers in text; the round budget is still
    # exhausted (rounds_used == max_turns), so this is a capped exit.
    fake_stream = FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("t1", "ws_create_file", {"path": "x.txt", "content": "x"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Out of turns."), *msg_end(stop_reason="end_turn")],
        ]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )

    cfg = AgentConfig(agent_type="general", read_only=False, max_turns=2, deadline_seconds=None)
    result = asyncio.run(
        run_agent(
            "create x.txt",
            config=cfg,
            session_id="",
            model_id_override="test-model",
            isolation="worktree",
        )
    )

    assert result.status == "completed"
    assert result.stopped_early is True
    # Not applied: the shared workspace (repo) must NOT have the new file —
    # the worktree is kept for inspection, per _agent_finished's gate.
    assert not (repo / "x.txt").exists()


# ── a custom/ephemeral agent type still resolves and runs correctly ────────


def test_ephemeral_agent_type_resolves_through_new_loop(monkeypatch):
    """server/agents/custom_config.py's ephemeral-type resolution
    (register_ephemeral_type -> get_agent_config) must still drive the
    NEW inner loop correctly: the resolved system_prompt/tools whitelist
    actually reach the request, and the returned AgentResult carries the
    ephemeral definition's clean display name."""
    from server.agents.custom_config import register_ephemeral_type

    resolution_key, ephemeral_config, meta = register_ephemeral_type(
        "sess-ephemeral",
        {
            "name": "readme_summarizer",
            "description": "summarizes docs",
            "tools": ["ws_read_file"],
            "read_only": True,
            "system_prompt": "You summarize README files concisely.",
        },
    )
    assert ephemeral_config.read_only is True
    assert meta["name"] == "readme_summarizer"

    fake_stream = FakeBedrockClient(
        [[msg_start(), *text_block("Summary: it's a demo repo."), *msg_end(stop_reason="end_turn")]]
    )
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    _mock_tools = [
        {"name": "ws_read_file", "description": "d", "input_schema": {"type": "object"}},
        {"name": "ws_write_file", "description": "d", "input_schema": {"type": "object"}},
    ]
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool",
        lambda *a, **k: (_mock_tools, [], len(_mock_tools)),
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    result = asyncio.run(
        run_agent(
            "summarize the README",
            agent_type=resolution_key,
            session_id="sess-ephemeral",
            model_id_override="test-model",
        )
    )

    assert result.status == "completed"
    # The AgentResult shows the ephemeral definition's clean display name,
    # never the synthesized resolution key.
    assert result.agent_type == "readme_summarizer"
    assert "Summary: it's a demo repo." in result.output

    # The ephemeral system_prompt reached the new adapter's request...
    assert "You summarize README files concisely." in fake_stream.requests[0]["system"]
    # ...and its read_only + tools whitelist actually restricted the pool —
    # write tools never reached the model, only the whitelisted read tool.
    sent_tool_names = {t["name"] for t in fake_stream.requests[0].get("tools", [])}
    assert sent_tool_names == {"ws_read_file"}
