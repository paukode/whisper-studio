"""Where the Bypass sandbox scope reaches, and where it stops.

server/security/bypass_sandbox.py runs a Bypass turn's commands with no
sandbox-exec. These tests drive the production call sites: a real shell
hook through run_hooks, terminal_run's hidden shell, a cron spawn, a
workflow launch, the approval floor and a card's yes. A read-only scope
(a read-only agent, a plan-mode call) keeps the sandbox under Bypass.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import tempfile

import pytest

from server import sandbox
from server.agents.tool_access import read_only_scope
from server.approval.bootstrap import register_defaults
from server.approval.markers import approval_request
from server.security import bypass_sandbox
from server.security.permissions import MODE_BYPASS, MODE_DEFAULT, approval_floor

register_defaults()

needs_sandbox_exec = pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)

PS = "/bin/ps -p $$ -o pid="


@pytest.fixture
def dirs():
    """A workspace in temp and a probe folder under $HOME, outside every
    write root of a confined run."""
    ws = tempfile.mkdtemp(prefix="whisper-scope-ws-")
    probe = tempfile.mkdtemp(prefix="whisper-scope-probe-", dir=os.path.expanduser("~"))
    try:
        yield ws, probe
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(probe, ignore_errors=True)


# ── hooks ────────────────────────────────────────────────────────────────────


@needs_sandbox_exec
def test_a_shell_hook_runs_with_the_turns_scope(dirs, monkeypatch):
    from server.hooks import engine
    from server.hooks.schema import HookDef

    ws, probe = dirs
    target = os.path.join(probe, "hooked")
    hook = HookDef(event="PreToolUse", command=f"echo hi > {target}").clamp()
    monkeypatch.setattr(engine, "merged_for_event", lambda event, ws: [hook])

    async def fire(unsandboxed):
        with bypass_sandbox.entered(unsandboxed):
            await engine.run_hooks("PreToolUse", {}, tool_name="probe", workspace=ws)

    asyncio.run(fire(False))
    assert not os.path.exists(target)
    asyncio.run(fire(True))
    assert os.path.exists(target)


# ── terminal_run's hidden shell ──────────────────────────────────────────────


@needs_sandbox_exec
def test_terminal_run_hidden_mode_follows_the_scope(dirs, monkeypatch):
    from server.executors import terminal_run

    ws, _probe = dirs
    monkeypatch.setattr(terminal_run, "_confining_workspace", lambda: ws)
    payload = {"command": PS, "mode": "sandbox", "timeout": 20}

    async def run(unsandboxed):
        with bypass_sandbox.entered(unsandboxed):
            return await terminal_run.do_terminal_run(dict(payload))

    ok, confined = asyncio.run(run(False))
    assert ok and "exit_code: 0" not in confined
    ok, free = asyncio.run(run(True))
    assert ok and "exit_code: 0" in free


# ── unattended and detached starts ───────────────────────────────────────────


def test_a_cron_spawn_starts_with_the_scope_off(monkeypatch):
    import server.cron_scheduler as sched
    from server.infrastructure import async_tasks

    seen = {}

    def fake_spawn(coro, *, name=None, context=None):
        seen["active"] = context.run(bypass_sandbox.active)
        coro.close()

    async def go():
        monkeypatch.setattr(sched, "_server_loop", asyncio.get_running_loop())
        monkeypatch.setattr(async_tasks, "spawn", fake_spawn)
        with bypass_sandbox.entered(True):
            sched._spawn_cron_run("job-x")

    asyncio.run(go())
    assert seen == {"active": False}


@pytest.fixture
def isolated_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    from server.infrastructure import sessions
    from server.tasks import registry
    from server.workflows import manager

    storage = tmp_path / "storage"
    os.makedirs(storage, exist_ok=True)
    for mod in (sessions, registry):
        monkeypatch.setattr(mod, "STORAGE_DIR", str(storage))
        monkeypatch.setattr(mod, "DB_PATH", str(storage / "sessions.db"))
    manager._live.clear()
    manager._task_ids.clear()
    yield manager
    manager._live.clear()
    manager._task_ids.clear()


def test_a_workflow_launch_takes_the_launchers_scope(isolated_runs, monkeypatch):
    manager = isolated_runs
    seen: list[bool] = []
    monkeypatch.setattr(manager, "_drive", lambda run, unsandboxed: seen.append(unsandboxed))
    monkeypatch.setattr(manager, "_schedule", lambda coro, run_id, spawn: coro.close())
    src = "export const meta = { name: 'n', description: 'd', phases: [] }\nreturn 1\n"

    with bypass_sandbox.entered(True):
        manager.start_run(src, session_id="s1")
    manager.start_run(src, session_id="s1")
    with bypass_sandbox.entered(True):
        read_token = read_only_scope.set(True)
        try:
            manager.start_run(src, session_id="s1")
        finally:
            read_only_scope.reset(read_token)
    assert seen == [True, False, False]


# ── the read-only scope keeps the sandbox ────────────────────────────────────


@needs_sandbox_exec
def test_a_read_only_scope_keeps_the_sandbox_in_bypass(dirs):
    ws, probe = dirs
    target = os.path.join(probe, "ro")
    with bypass_sandbox.entered(True):
        token = read_only_scope.set(True)
        try:
            assert not bypass_sandbox.active()
            assert bypass_sandbox.held_back()
            write = sandbox.run_sandboxed(
                f"echo hi > {target}", cwd=ws, timeout=20, write_mode="readonly"
            )
        finally:
            read_only_scope.reset(token)
        assert bypass_sandbox.active()
    assert write.returncode != 0
    assert not os.path.exists(target)


def test_a_read_only_scope_refuses_danger_full_access_in_bypass():
    from server.executors.code import _python_write_mode
    from server.executors.terminal_run import _resolve_sandbox_permissions

    escalation = {"sandbox_permissions": "danger-full-access", "justification": "why"}
    with bypass_sandbox.entered(True):
        token = read_only_scope.set(True)
        try:
            assert _resolve_sandbox_permissions(escalation)[2] == bypass_sandbox.HELD_BACK_REFUSAL
            assert _python_write_mode(escalation)[1] == bypass_sandbox.HELD_BACK_REFUSAL
        finally:
            read_only_scope.reset(token)
    # Outside Bypass a person reads the justification on the card.
    assert _resolve_sandbox_permissions(escalation)[0] == "danger-full-access"
    assert _python_write_mode(escalation) == ("open", "")


# ── approval floor and cards ─────────────────────────────────────────────────


def test_bypass_asks_no_card_for_danger_full_access():
    escalation = {"command": "echo hi", "sandbox_permissions": "danger-full-access"}
    assert approval_floor("terminal_run", escalation, "cli", MODE_DEFAULT) == "sandbox-escalation"
    assert approval_floor("run_python", escalation, "python", MODE_DEFAULT) == "sandbox-escalation"
    assert approval_floor("terminal_run", escalation, "cli", MODE_BYPASS) is None
    assert approval_floor("run_python", escalation, "python", MODE_BYPASS) is None


class _State:
    def __init__(self, payload):
        self.tool_id = "tu_1"
        self.tool_name = "run_python"
        self.output = approval_request(json.dumps(payload))
        self.side_effects = []
        self.status = "pending"
        self.duration_ms = 0
        self.stats = None
        self.ws_root_taken = False
        self.ws_root = None


def _card_payload(payload, *, unsandboxed):
    """The payload of the card the gate raises for ``payload`` in default
    mode, in a turn with or without the Bypass scope."""
    from server.tool_executor import process_tool_results

    async def go():
        with bypass_sandbox.entered(unsandboxed):
            return await process_tool_results(
                [_State(payload)],
                budget_fn=lambda _n, out: out,
                config={},
                model_id="",
                recent_messages=[],
                mode=MODE_DEFAULT,
                unattended=False,
            )

    _results, events, pending, _q = asyncio.run(go())
    assert pending
    cards = [json.loads(e)["approval_request"] for e in events if "approval_request" in e]
    return cards[0]["payload"]


def test_a_card_carries_the_scope_of_the_turn_that_raised_it():
    key = bypass_sandbox.UNSANDBOXED_KEY
    forged = {"action": "run_python", "code": "print(1)", "session_id": "s1", key: True}
    assert key not in _card_payload(forged, unsandboxed=False)
    assert _card_payload(forged, unsandboxed=True)[key] is True


def test_a_cards_yes_runs_with_its_stamp_not_the_live_mode(monkeypatch):
    from server.approval import spec as spec_mod
    from server.approval.spec import ApprovalOutcome, execute_approved_by_human

    seen: list[bool] = []

    class _Spec:
        category = "cli"
        on_approved_by_human = None

        async def executor(self, payload):
            seen.append(bypass_sandbox.active())
            return ApprovalOutcome(ok=True)

    monkeypatch.setattr(spec_mod, "stale_workspace_refusal", lambda spec, payload: None)
    monkeypatch.setattr("server.security.permissions.get_mode", lambda: MODE_BYPASS)
    key = bypass_sandbox.UNSANDBOXED_KEY
    asyncio.run(execute_approved_by_human(_Spec(), {"command": "x"}))
    asyncio.run(execute_approved_by_human(_Spec(), {"command": "x", key: True}))
    asyncio.run(execute_approved_by_human(_Spec(), {"command": "x", key: "yes"}))
    assert seen == [False, True, False]
