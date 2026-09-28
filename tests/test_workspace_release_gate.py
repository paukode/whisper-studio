"""A released turn cannot reach the workspace through any door.

tests/test_workspace_disconnect_midturn.py pins the contract for tools the
batch runs directly. These pin the rest of it: the gate that processes a
batch's results (where pre-approved and unattended actions actually execute,
after the batch's own latch is gone), the turns that are not chat turns
(subagents, scheduled runs, voice and other headless runs), commands whose
cwd falls back to the workspace, and tools that would connect a folder
themselves and so undo the user's disconnect.

Released means the user disconnected the turn's workspace, or connected a
different one from the UI, while the turn was running.
"""

import asyncio
import json
import os
import subprocess

import pytest

import server.executors.terminal_run  # noqa: F401 - registers terminal_run / terminal_send
import server.git.executor  # noqa: F401 - registers the git tool executors
import server.tool_executor as te
import server.workspace.executors  # noqa: F401 - registers the ws_* executors
import server.workspace.state as state
from server.workspace.state import connect_workspace, latch_workspace
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block

SESSION = "sess-release-gate"


def _make_ws(root, name: str):
    ws = root / name
    (ws / "build").mkdir(parents=True)
    (ws / "a.txt").write_text(f"{name} original\n")
    (ws / "build" / "out.txt").write_text(f"{name} build\n")
    return ws


@pytest.fixture
def ws_a(tmp_path):
    return _make_ws(tmp_path, "ws_a")


@pytest.fixture
def ws_b(tmp_path):
    return _make_ws(tmp_path, "ws_b")


def _git(ws, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_repo(ws) -> None:
    _git(ws, "init", "-q", "-b", "main")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "init")
    (ws / "new.txt").write_text("uncommitted\n")


def _uses(calls):
    return [{"id": f"t{i}", "name": n, "input": dict(inp)} for i, (n, inp) in enumerate(calls)]


async def _batch(tool_uses, latch, *, unattended=False):
    from concurrent.futures import ThreadPoolExecutor

    from server.approval.bootstrap import register_defaults

    register_defaults()
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        return await te.execute_tool_batch(
            tool_uses,
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=pool,
            transcript="",
            attachments=None,
            session_id=SESSION,
            session_denials={},
            model_id="",
            plan_mode=False,
            unattended=unattended,
            workspace_latch=latch,
        )
    finally:
        pool.shutdown(wait=False)


async def _gate(states, latch, *, approvals=None, mode="default", model_id="", unattended=False):
    return await te.process_tool_results(
        states,
        budget_fn=lambda _name, out: out,
        session_approvals=dict(approvals or {}),
        config={},
        model_id=model_id,
        mode=mode,
        unattended=unattended,
        workspace_latch=latch,
    )


def _run(calls, latch, **gate_kwargs):
    async def go():
        states = await _batch(_uses(calls), latch, unattended=gate_kwargs.get("unattended", False))
        return states, await _gate(states, latch, **gate_kwargs)

    return asyncio.run(go())


def _cards(sse):
    return [json.loads(e)["approval_request"] for e in sse if "approval_request" in e]


# ── The gate: pre-approved, unattended and carded actions ─────────────────


@pytest.mark.parametrize("approvals", [{"cli": "allow"}, {}], ids=["pre-approved", "card"])
def test_a_git_commit_is_refused_after_a_switch(ws_a, ws_b, approvals):
    """The git write tools emit a bare approval sentinel without resolving the
    workspace, so only the gate can refuse them. Pre-approved, the commit ran
    in B; carded, the card was stamped with B and its Yes ran there too."""
    _make_repo(ws_a)
    _make_repo(ws_b)
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    connect_workspace(str(ws_b), by_user=True)

    states, (results, sse, pending, _q) = _run(
        [("git_add_commit", {"message": "m", "all": True})], latch, approvals=approvals
    )

    assert results[0]["content"].startswith("No workspace connected: the user disconnected")
    assert pending is False and _cards(sse) == []
    assert _git(ws_b, "rev-list", "--count", "HEAD") == "1", "the commit landed in B"
    assert _git(ws_a, "rev-list", "--count", "HEAD") == "1"


@pytest.mark.parametrize(
    "calls",
    [
        [("git_push", {})],
        [("git_checkout", {"branch": "main"})],
        [("preview_start", {"session_name": "web", "runtimeExecutable": "true"})],
    ],
    ids=lambda c: c[0][0],
)
def test_other_sentinel_only_workspace_actions_are_refused_after_a_switch(ws_a, ws_b, calls):
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    connect_workspace(str(ws_b), by_user=True)

    _states, (results, sse, pending, _q) = _run(calls, latch)

    assert results[0]["content"].startswith("No workspace connected"), results[0]["content"]
    assert pending is False and _cards(sse) == []


@pytest.mark.parametrize("approvals", [{"delete": "allow"}, {}], ids=["pre-approved", "card"])
def test_a_switch_between_the_batch_and_the_gate_is_refused(ws_a, ws_b, approvals):
    """The batch runs in A and returns a delete sentinel for a relative path;
    the user switches to B before the gate processes it (the rest of the
    batch, the risk explanation and the classifier all run in between)."""
    connect_workspace(str(ws_a))
    latch = latch_workspace()

    async def go():
        states = await _batch(_uses([("ws_delete_file", {"path": "build/out.txt"})]), latch)
        assert states[0].output.startswith("[WS_APPROVAL]")
        state.disconnect_workspace()
        connect_workspace(str(ws_b), by_user=True)
        return await _gate(states, latch, approvals=approvals)

    results, sse, pending, _q = asyncio.run(go())

    assert results[0]["content"].startswith("No workspace connected")
    assert pending is False and _cards(sse) == []
    assert (ws_b / "build" / "out.txt").exists(), "B's file was deleted"
    assert (ws_a / "build" / "out.txt").exists()


def test_a_switch_while_the_auto_mode_classifier_runs_is_refused(ws_a, ws_b, monkeypatch):
    """The last check sits right before the executor, so a switch during the
    classifier's own await cannot run A's payload in B."""
    connect_workspace(str(ws_a))
    latch = latch_workspace()

    async def classify_while_the_user_switches(*args, **kwargs):
        connect_workspace(str(ws_b), by_user=True)
        return {"decision": "allow"}

    monkeypatch.setattr(te, "classify_tool_call", classify_while_the_user_switches)

    _states, (results, _sse, pending, _q) = _run(
        [("ws_delete_file", {"path": "build/out.txt"})], latch, mode="auto", model_id="m"
    )

    assert results[0]["content"].startswith("No workspace connected"), results[0]["content"]
    assert pending is False
    assert (ws_b / "build" / "out.txt").exists(), "B's file was deleted"


def test_a_card_keeps_the_root_its_tool_ran_in(ws_a, ws_b):
    """A tool connecting a folder mid-turn does not release the turn, but a
    payload resolved in A still belongs to A: no card offers it in B."""
    connect_workspace(str(ws_a))
    latch = latch_workspace()

    async def go():
        states = await _batch(_uses([("ws_delete_file", {"path": "build/out.txt"})]), latch)
        connect_workspace(str(ws_b))  # not by_user: a tool did it
        return await _gate(states, latch)

    results, sse, pending, _q = asyncio.run(go())

    assert _cards(sse) == [] and pending is False
    assert "out of date" in results[0]["content"]
    assert os.path.realpath(ws_a) in results[0]["content"]
    assert (ws_b / "build" / "out.txt").exists()


def test_a_card_in_an_unchanged_workspace_is_still_raised(ws_a):
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    _states, (_results, sse, pending, _q) = _run(
        [("ws_delete_file", {"path": "build/out.txt"})], latch
    )
    cards = _cards(sse)
    assert pending is True and len(cards) == 1
    assert cards[0]["payload"]["workspace_root"] == os.path.realpath(ws_a)


def test_an_unattended_turn_is_refused_after_a_switch(ws_a, ws_b):
    """Unattended turns auto-approve every sentinel, so this is the gate's
    only chance to refuse one."""
    connect_workspace(str(ws_a))
    latch = latch_workspace()

    async def go():
        states = await _batch(
            _uses([("ws_delete_file", {"path": "build/out.txt"})]), latch, unattended=True
        )
        connect_workspace(str(ws_b), by_user=True)
        return await _gate(states, latch, unattended=True)

    results, _sse, _pending, _q = asyncio.run(go())
    assert results[0]["content"].startswith("No workspace connected")
    assert (ws_b / "build" / "out.txt").exists(), "B's file was deleted"


# ── Commands whose cwd falls back to the workspace ────────────────────────


def test_a_terminal_cwd_that_is_not_a_directory_is_workspace_bound(ws_a, ws_b):
    from server.approval import registry
    from server.approval.bootstrap import register_defaults

    register_defaults()
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    connect_workspace(str(ws_b), by_user=True)
    missing = str(ws_a / "missing")

    _states, (results, sse, _pending, _q) = _run(
        [("terminal_run", {"command": "touch made_here", "cwd": missing})],
        latch,
        approvals={"cli": "allow"},
    )
    assert results[0]["content"].startswith("No workspace connected"), results[0]["content"]
    assert not (ws_b / "made_here").exists()

    # terminal_send opens its terminal in the same fallback cwd.
    _states, (results, sse, pending, _q) = _run([("terminal_send", {"input": "ls"})], latch)
    assert results[0]["content"].startswith("No workspace connected"), results[0]["content"]
    assert pending is False and _cards(sse) == []

    for action in ("terminal_run", "terminal_send"):
        spec = registry.get(action)
        assert spec.binds_workspace({"cwd": ""})
        assert spec.binds_workspace({"cwd": missing})
        assert not spec.binds_workspace({"cwd": str(ws_a)})


# ── A released turn cannot connect a folder itself ───────────────────────


def test_a_released_turn_cannot_reopen_the_folder_the_user_let_go_of(ws_a, monkeypatch):
    """ws_open_folder is how a model recovers from "No workspace connected",
    and under a folder grant it connects silently, undoing the disconnect."""
    monkeypatch.setattr("server.security.folder_grants.needs_grant", lambda *a, **k: False)
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    state.disconnect_workspace()

    _states, (results, _sse, _pending, _q) = _run([("ws_open_folder", {"path": str(ws_a)})], latch)

    assert "No workspace connected" in json.loads(results[0]["content"])["error"]
    assert state.load_workspace_config().get("path") is None


def test_a_released_turn_cannot_connect_a_workspace_through_a_tool(ws_a, ws_b):
    connect_workspace(str(ws_a))
    latch = latch_workspace()
    state.disconnect_workspace()

    token = state.set_turn_latch(latch)
    try:
        with pytest.raises(ValueError, match="No workspace connected"):
            connect_workspace(str(ws_b))
        opened = json.loads(server.workspace.executors.open_folder_now(os.path.realpath(ws_b)))
        assert "No workspace connected" in opened["error"]
        # The user's own connect is never refused.
        assert connect_workspace(str(ws_b), by_user=True) == os.path.realpath(ws_b)
    finally:
        state.reset_turn_latch(token)


# ── Turns started from a released turn inherit its release ───────────────


def test_a_turn_started_inside_a_released_turn_inherits_its_latch(ws_a, ws_b, tmp_path):
    connect_workspace(str(ws_a))
    parent = latch_workspace()
    connect_workspace(str(ws_b), by_user=True)

    token = state.set_turn_latch(parent)
    try:
        child = latch_workspace()
        assert child is parent
        pinned = tmp_path / "pinned"
        pinned.mkdir()
        pin = state.set_workspace_override(str(pinned))
        try:
            assert latch_workspace().path == str(pinned), "a pinned run keeps its own root"
        finally:
            state.reset_workspace_override(pin)
    finally:
        state.reset_turn_latch(token)

    token = state.set_turn_latch(child)
    try:
        assert state.get_workspace_path() is None
        assert state.workspace_lost_message().startswith("No workspace connected")
    finally:
        state.reset_turn_latch(token)


def _patch_agent_model(monkeypatch, fake_bedrock):
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_bedrock)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )


def _delete_then_stop():
    return FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("tu_1", "ws_delete_file", {"path": "build/out.txt"}),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")],
        ]
    )


def _read_and_delete_then_stop():
    return FakeBedrockClient(
        [
            [
                msg_start(),
                *tool_use_block("tu_1", "ws_read_file", {"path": "a.txt"}),
                *tool_use_block("tu_2", "ws_delete_file", {"path": "build/out.txt"}, index=1),
                *msg_end(stop_reason="tool_use"),
            ],
            [msg_start(), *text_block("Done."), *msg_end(stop_reason="end_turn")],
        ]
    )


def _replayed_result(fake_bedrock) -> str:
    return json.dumps(fake_bedrock.requests[1]["messages"][-1]["content"])


@pytest.mark.parametrize("isolation", [None, "worktree"])
def test_a_subagent_spawned_by_a_released_turn_cannot_touch_the_new_workspace(
    ws_a, ws_b, monkeypatch, isolation
):
    """The agent is unattended, so every write is auto-approved. It used to
    take a fresh latch that read no workspace (so was never released) and
    then read B's files; an isolated one forked a worktree of B."""
    import server.agents.runtime as rt
    from server.approval.bootstrap import register_defaults
    from server.workspace.executors import _WORKTREES

    register_defaults()
    forked = []
    monkeypatch.setattr(rt, "_enter_agent_worktree", lambda *a: forked.append(a))
    fake_bedrock = _read_and_delete_then_stop()
    _patch_agent_model(monkeypatch, fake_bedrock)
    connect_workspace(str(ws_a))
    parent = latch_workspace()
    connect_workspace(str(ws_b), by_user=True)

    async def spawn_from_the_parents_batch():
        token = state.set_turn_latch(parent)
        try:
            return await rt.run_agent(
                "clean the build",
                agent_type="general",
                session_id="",
                model_id_override="test-model",
                isolation=isolation,
            )
        finally:
            state.reset_turn_latch(token)

    try:
        asyncio.run(spawn_from_the_parents_batch())
    finally:
        _WORKTREES.clear()

    replayed = _replayed_result(fake_bedrock)
    assert "ws_b original" not in replayed, "the subagent read B's file"
    assert (ws_b / "build" / "out.txt").exists(), "the subagent deleted B's file"
    # Both calls were refused: by the latch, or, once an isolated run falls
    # back to read-only, the delete by the agent's tool scope before it.
    results = [r["content"] for r in fake_bedrock.requests[1]["messages"][-1]["content"]]
    assert len(results) == 2, replayed
    assert all(r.startswith(("No workspace connected", "[Refused]")) for r in results), replayed
    assert forked == [], "an isolated subagent forked the newly connected workspace"


# ── Scheduled and headless (voice) runs are latched too ──────────────────


def _switch_then_route(ws_b):
    """route_tool that lets the user switch to B just before the tool runs."""
    real_route = te.route_tool

    async def route(name, tool_input, **kwargs):
        connect_workspace(str(ws_b), by_user=True)
        return await real_route(name, tool_input, **kwargs)

    return route


def test_a_scheduled_run_is_refused_after_a_switch(ws_a, ws_b, monkeypatch):
    import server.cron_history as H
    import server.cron_scheduler as C
    import server.infrastructure.config as CFG
    import server.prompts.rules as R
    from server.approval.bootstrap import register_defaults

    register_defaults()
    job = {
        "id": "job-release",
        "name": "release-job",
        "prompt": "clean the build",
        "session_id": "sess-cron-release",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }
    fake_bedrock = _delete_then_stop()
    monkeypatch.setattr(C, "load_cron_jobs", lambda: [job])
    _patch_agent_model(monkeypatch, fake_bedrock)
    monkeypatch.setattr(R, "append_rules", lambda s: s)
    monkeypatch.setattr(H, "start_run", lambda *a, **k: None)
    monkeypatch.setattr(te, "route_tool", _switch_then_route(ws_b))
    monkeypatch.setattr(
        CFG,
        "load_config",
        lambda: {
            "chat_models": {"haiku": "fake-model-id"},
            "cron_max_runs_per_job": 200,
            "cron_misfire_grace_sec": 3600,
            "feature_flags": {"cron_verify": False},
        },
    )
    recorded: dict = {}

    def fake_push(job, text, status="ok", *, run_id, duration_ms=None):
        recorded["status"] = status

    monkeypatch.setattr(C, "_push_result", fake_push)
    connect_workspace(str(ws_a))

    asyncio.run(C._execute_cron_prompt(job["id"]))

    assert recorded.get("status") == "ok"
    assert (ws_b / "build" / "out.txt").exists(), "the scheduled run deleted B's file"
    assert "No workspace connected" in _replayed_result(fake_bedrock)


def test_a_voice_run_is_refused_instead_of_paused_after_a_switch(ws_a, ws_b, monkeypatch):
    """Voice turns are attended headless runs: unlatched, the delete raised a
    card for B and paused the run."""
    from server.approval.bootstrap import register_defaults
    from server.exec.headless import run_headless_turn
    from server.infrastructure import config as config_mod

    register_defaults()
    fake_bedrock = _delete_then_stop()
    _patch_agent_model(monkeypatch, fake_bedrock)
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"chat_models": {"sonnet": "test-model"}, "default_chat_model": "sonnet"},
    )
    monkeypatch.setattr(te, "route_tool", _switch_then_route(ws_b))
    connect_workspace(str(ws_a))

    async def collect():
        return [
            ev
            async for ev in run_headless_turn(
                "clean the build",
                model_key="sonnet",
                ephemeral=True,
                attended=True,
                session_id="sess-voice-release",
            )
        ]

    events = asyncio.run(collect())

    kinds = [e["type"] for e in events]
    assert "approval_request" not in kinds and "workspace_prompt" not in kinds, kinds
    assert events[-1]["status"] == "completed", events[-1]
    assert (ws_b / "build" / "out.txt").exists()
    assert "No workspace connected" in _replayed_result(fake_bedrock)
