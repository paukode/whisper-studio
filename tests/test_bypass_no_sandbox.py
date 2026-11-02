"""Bypass runs commands with no OS sandbox (server/security/bypass_sandbox.py).

The user's rule (2026-11-02): in bypassPermissions a command runs the way
Claude Code's bypass runs it, with no sandbox-exec and no write limits.
Every other mode keeps the sandbox, and so does a run no Bypass turn
started (cron). The kernel checks below run the same command both ways and
compare the outcomes: setuid /bin/ps, a nested sandbox-exec with a deny
rule, and a write outside the workspace.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import platform
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import sandbox
from server.approval.bootstrap import register_defaults
from server.approval.markers import approval_request
from server.security import bypass_sandbox
from server.security.permissions import MODE_BYPASS, MODE_DEFAULT

register_defaults()

needs_sandbox_exec = pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)

PS = "/bin/ps -p $$ -o pid="
NESTED = (
    "sandbox-exec -p '(version 1)(allow default)"
    '(deny file-write* (subpath "/private/var/empty"))\' /bin/echo nested-ran'
)


@pytest.fixture
def dirs():
    """A workspace in temp and a probe folder under $HOME, outside every
    write root of a confined run."""
    ws = tempfile.mkdtemp(prefix="whisper-bypass-ws-")
    probe = tempfile.mkdtemp(prefix="whisper-bypass-probe-", dir=os.path.expanduser("~"))
    try:
        yield ws, probe
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(probe, ignore_errors=True)


# ── the scope ────────────────────────────────────────────────────────────────


def test_the_scope_only_turns_on_down_a_chain():
    assert not bypass_sandbox.active()
    with bypass_sandbox.entered(True):
        assert bypass_sandbox.active()
        with bypass_sandbox.entered(False):
            assert bypass_sandbox.active()
    assert not bypass_sandbox.active()
    assert bypass_sandbox.is_bypass(MODE_BYPASS)
    assert not bypass_sandbox.is_bypass(MODE_DEFAULT)


def test_a_cron_context_starts_with_the_scope_off():
    with bypass_sandbox.entered(True):
        context = contextvars.copy_context()
    bypass_sandbox.set_off(context)
    assert context.run(bypass_sandbox.active) is False


# ── the kernel: same command, both ways ──────────────────────────────────────


@needs_sandbox_exec
def test_default_mode_confines_and_bypass_does_not(dirs):
    ws, probe = dirs
    target = os.path.join(probe, "written")
    for unsandboxed in (False, True):
        with bypass_sandbox.entered(unsandboxed):
            ps = sandbox.run_sandboxed(PS, cwd=ws, timeout=20, write_mode="workspace")
            nested = sandbox.run_sandboxed(NESTED, cwd=ws, timeout=20, write_mode="workspace")
            write = sandbox.run_sandboxed(
                f"echo hi > {target}", cwd=ws, timeout=20, write_mode="workspace"
            )
        assert (ps.returncode == 0) is unsandboxed
        assert ("nested-ran" in nested.stdout) is unsandboxed
        assert (write.returncode == 0) is unsandboxed
        assert os.path.exists(target) is unsandboxed


@needs_sandbox_exec
def test_a_bypass_background_shell_has_no_profile(dirs, tmp_path):
    ws, _probe = dirs
    out = tmp_path / "out.txt"
    with bypass_sandbox.entered(True), open(out, "w") as f:
        proc, profile = sandbox.popen_sandboxed(PS, cwd=ws, stdout_file=f, write_mode="readonly")
        assert profile is None
        assert "sandbox-exec" not in proc.args
        assert proc.wait(timeout=20) == 0
    with open(out, "w") as f:
        proc, profile = sandbox.popen_sandboxed(PS, cwd=ws, stdout_file=f, write_mode="readonly")
        try:
            assert proc.args[0] == "sandbox-exec"
            assert proc.wait(timeout=20) != 0
        finally:
            os.unlink(profile)


def test_a_bypass_pty_shell_is_not_wrapped():
    shell = ["/bin/bash", "--noprofile", "--norc"]
    with bypass_sandbox.entered(True):
        argv, profile = sandbox.build_pty_sandbox_wrap(shell, None, write_mode="workspace")
        assert sandbox.pty_sandbox_backend() == "bypass"
    assert argv == shell and profile is None


def test_bypass_needs_no_sandbox_exec(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_is_sandbox_exec_available", lambda: False)
    with pytest.raises(sandbox.SandboxUnavailableError):
        sandbox.run_sandboxed("true", cwd=str(tmp_path))
    with bypass_sandbox.entered(True):
        assert sandbox.run_sandboxed("true", cwd=str(tmp_path)).returncode == 0


@needs_sandbox_exec
def test_ws_run_command_in_bypass_runs_ps(dirs):
    from server.workspace.executors import run_workspace_command

    ws, _probe = dirs
    confined = run_workspace_command(PS, ws, write_mode="readonly")
    with bypass_sandbox.entered(True):
        free = run_workspace_command(PS, ws, write_mode="readonly")
    assert "Operation not permitted" in confined.text
    assert "Operation not permitted" not in free.text
    assert "exit code" not in free.text


# ── who sets the scope ───────────────────────────────────────────────────────


def _batch(mode, *, unattended=False):
    from server import tool_executor as te

    async def run():
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            return await te.execute_tool_batch(
                [{"id": "a", "name": "probe", "input": {}}],
                tool_schemas={"probe": {"type": "object", "properties": {}}},
                is_concurrent_safe=lambda n: False,
                loop=asyncio.get_running_loop(),
                executor=executor,
                transcript="",
                attachments=None,
                session_id="s1",
                session_denials={},
                model_id="",
                plan_mode=False,
                mode=mode,
                unattended=unattended,
            )
        finally:
            executor.shutdown(wait=True)

    return run


def test_the_tool_batch_sets_the_scope_from_the_mode(monkeypatch):
    from server import tool_executor as te

    seen: list[bool] = []

    async def route(name, tool_input, **kwargs):
        seen.append(bypass_sandbox.active())
        return "ok", []

    monkeypatch.setattr(te, "route_tool", route)
    asyncio.run(_batch(MODE_DEFAULT)())
    asyncio.run(_batch(MODE_BYPASS)())

    # An agent's own turn has mode default; started by a Bypass call, it
    # inherits the scope.
    async def agent_under_bypass():
        with bypass_sandbox.entered(True):
            await _batch(MODE_DEFAULT, unattended=True)()

    asyncio.run(agent_under_bypass())
    assert seen == [False, True, True]
    assert not bypass_sandbox.active()


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


def _gate(payload, *, mode, unattended=False):
    from server.tool_executor import process_tool_results

    results, _events, _pending, _q = asyncio.run(
        process_tool_results(
            [_State(payload)],
            budget_fn=lambda _n, out: out,
            config={},
            model_id="",
            recent_messages=[],
            mode=mode,
            unattended=unattended,
        )
    )
    return json.dumps(results)


@needs_sandbox_exec
def test_run_python_run_inline_follows_the_mode(dirs, monkeypatch):
    """A Bypass run_python runs inline with no sandbox; an agent's in default
    mode runs inline confined. Same script, both ways."""
    ws, probe = dirs
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: ws)
    target = os.path.join(probe, "py")
    code = (
        "import subprocess\n"
        "r = subprocess.run(['/bin/ps', '-p', '1'], capture_output=True)\n"
        "print('PS', r.returncode)\n"
        f"open({target!r}, 'w').write('x')\n"
    )
    payload = {"action": "run_python", "code": code, "session_id": "s1"}
    confined = _gate(payload, mode=MODE_DEFAULT, unattended=True)
    assert "PS 0" not in confined and not os.path.exists(target)
    free = _gate(payload, mode=MODE_BYPASS)
    assert "PS 0" in free and os.path.exists(target)


def test_danger_full_access_is_accepted_and_ignored_in_bypass():
    from server.executors.code import _python_write_mode
    from server.executors.terminal_run import _resolve_sandbox_permissions

    agent_escalation = {"sandbox_permissions": "danger-full-access", "__agent__": True}
    assert _resolve_sandbox_permissions(agent_escalation)[2]
    assert _python_write_mode(agent_escalation)[1]
    with bypass_sandbox.entered(True):
        assert _resolve_sandbox_permissions(agent_escalation)[2] == ""
        assert _python_write_mode(agent_escalation) == ("workspace", "")


def test_a_workflow_run_keeps_its_launchers_scope():
    from server.workflows import manager

    seen: list[bool] = []

    class _Run:
        run_id = "r1"
        workspace_path = None

        async def run(self):
            seen.append(bypass_sandbox.active())
            return {"status": "failed", "error": "probe"}

    finalized = []
    orig = manager._finalize
    manager._finalize = lambda run, outcome: finalized.append(outcome)
    try:
        asyncio.run(manager._drive(_Run(), True))
        asyncio.run(manager._drive(_Run(), False))
    finally:
        manager._finalize = orig
    assert seen == [True, False]
    assert len(finalized) == 2


def test_the_completion_gate_runs_under_the_turns_scope(monkeypatch):
    from server.goals import GateContext, GateDecision, gate

    seen: list[bool] = []

    async def body(ctx):
        seen.append(bypass_sandbox.active())
        return GateDecision(block=False)

    monkeypatch.setattr(gate, "_run_completion_gate", body)
    asyncio.run(gate.run_completion_gate(GateContext(session_id="s", unsandboxed=True)))
    asyncio.run(gate.run_completion_gate(GateContext(session_id="s")))
    assert seen == [True, False]
