"""No macOS sandbox-exec, no command: the sandbox fails closed.

sandbox-exec is the only backend. When it cannot be used, run_sandboxed,
popen_sandboxed and build_pty_sandbox_wrap raise SandboxUnavailableError in
every write mode, and nothing starts. Each caller turns that into its own
clear error (a tool result, a failed task, an HTTP error, a hook failure),
never a traceback and never a run without the sandbox.

Every test here makes subprocess.Popen and asyncio's subprocess spawn record
the call and fail, so a run that slips through is caught.
"""

import asyncio
import json
import os
import subprocess

import pytest

from server import sandbox
from server.sandbox import SANDBOX_UNAVAILABLE_MESSAGE, SandboxUnavailableError
from server.workspace import paths

WRITE_MODES = ["open", "workspace", "readonly"]


@pytest.fixture
def spawned(monkeypatch, tmp_path):
    """No sandbox-exec; every process spawn is recorded and refused."""
    calls = []

    def no_popen(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a process was started without the sandbox")

    async def no_exec(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a process was started without the sandbox")

    monkeypatch.setattr(sandbox, "_is_sandbox_exec_available", lambda: False)
    monkeypatch.setattr(subprocess, "Popen", no_popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_exec)
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path / "data"))
    return calls


@pytest.fixture
def isolated_tasks(tmp_path, monkeypatch):
    from server.tasks import registry, shell

    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    return tmp_path / "background_output"


# ── server/sandbox.py ─────────────────────────────────────────────────────


def test_the_error_says_the_command_did_not_run():
    err = SandboxUnavailableError()
    assert isinstance(err, RuntimeError)
    assert str(err) == SANDBOX_UNAVAILABLE_MESSAGE
    assert "NOT run" in str(err) and "sandbox-exec" in str(err)


def test_the_backends_report_none_without_sandbox_exec(monkeypatch):
    monkeypatch.setattr(sandbox, "_is_sandbox_exec_available", lambda: False)
    assert sandbox.sandbox_backend() == "none"
    assert sandbox.pty_sandbox_backend() == "none"
    monkeypatch.setattr(sandbox, "_is_sandbox_exec_available", lambda: True)
    assert sandbox.sandbox_backend() == "sandbox-exec"
    assert sandbox.pty_sandbox_backend() == "sandbox-exec"


def test_no_linux_backend_is_left():
    for name in ("_is_bwrap_available", "_bwrap_argv", "_run_bwrap_sandboxed", "_bwrap_deny_args"):
        assert not hasattr(sandbox, name), name
    assert set(sandbox._DENIAL_SIGNATURES) == {"sandbox-exec"}


@pytest.mark.parametrize("write_mode", WRITE_MODES)
def test_run_sandboxed_raises_and_runs_nothing(spawned, tmp_path, write_mode):
    with pytest.raises(SandboxUnavailableError):
        sandbox.run_sandboxed("touch made", cwd=str(tmp_path), write_mode=write_mode)
    assert spawned == []
    assert not (tmp_path / "made").exists()


@pytest.mark.parametrize("write_mode", WRITE_MODES)
def test_popen_sandboxed_raises_and_starts_nothing(spawned, tmp_path, write_mode):
    with open(tmp_path / "out.txt", "w") as out:
        with pytest.raises(SandboxUnavailableError):
            sandbox.popen_sandboxed(
                "true", cwd=str(tmp_path), stdout_file=out, write_mode=write_mode
            )
    assert spawned == []


@pytest.mark.parametrize("write_mode", WRITE_MODES)
def test_build_pty_sandbox_wrap_raises_instead_of_returning_the_bare_shell(
    spawned, tmp_path, write_mode, monkeypatch
):
    def no_profile(*a, **k):
        raise AssertionError("no profile is written without the sandbox")

    monkeypatch.setattr(sandbox.tempfile, "mkstemp", no_profile)
    with pytest.raises(SandboxUnavailableError):
        sandbox.build_pty_sandbox_wrap(["/bin/sh"], str(tmp_path), write_mode=write_mode)
    assert spawned == []


# ── terminal_run and terminal_send ────────────────────────────────────────


@pytest.mark.parametrize("agent", [False, True])
def test_terminal_run_refuses_instead_of_running_unsandboxed(spawned, agent):
    from server.executors.terminal_run import do_terminal_run

    payload = {"command": "echo hi", **({"__agent__": True} if agent else {})}
    ok, msg = asyncio.run(do_terminal_run(payload))
    assert ok is False
    assert msg == SANDBOX_UNAVAILABLE_MESSAGE
    assert spawned == []


def test_terminal_run_refuses_when_the_wrap_itself_fails(spawned, monkeypatch):
    """The up-front check passed but the wrap fails: still a clean refusal,
    and the PTY it opened is closed again."""
    from server.executors.terminal_run import do_terminal_run

    monkeypatch.setattr(sandbox, "pty_sandbox_backend", lambda: "sandbox-exec")
    before = len(os.listdir("/dev/fd"))
    ok, msg = asyncio.run(do_terminal_run({"command": "echo hi"}))
    assert ok is False
    assert msg == SANDBOX_UNAVAILABLE_MESSAGE
    assert spawned == []
    assert len(os.listdir("/dev/fd")) == before


def test_terminal_send_refuses_and_keeps_no_session(spawned):
    from server import terminal
    from server.executors.terminal_run import do_terminal_send

    ok, msg = asyncio.run(do_terminal_send({"session_id": "sess-fc", "input": "echo hi"}))
    assert ok is False
    assert msg == SANDBOX_UNAVAILABLE_MESSAGE
    assert "sess-fc" not in terminal._interactive_ptys
    assert spawned == []


# ── background tasks and ws_run_command ───────────────────────────────────


def test_a_background_task_is_recorded_as_failed(spawned, isolated_tasks, tmp_path):
    from server.tasks import registry, shell

    info = shell.start_shell_task("echo hi", cwd=str(tmp_path), session_id="sess-fc")
    assert info["status"] == "failed"
    assert info["error"] == SANDBOX_UNAVAILABLE_MESSAGE
    task = registry.get_task(info["task_id"])
    assert task["status"] == "failed"
    assert SANDBOX_UNAVAILABLE_MESSAGE in task["result_text"]
    # Nothing ran, so no empty output file is left behind.
    assert not os.path.exists(info["output_path"])
    assert not isolated_tasks.exists() or os.listdir(isolated_tasks) == []
    assert spawned == []


def test_the_handoff_raises_and_leaves_no_output_file(spawned, isolated_tasks, tmp_path):
    from server.tasks import handoff

    with pytest.raises(SandboxUnavailableError):
        handoff.run_with_handoff("echo hi", "echo hi", cwd=str(tmp_path), session_id="sess-fc")
    assert not isolated_tasks.exists() or os.listdir(isolated_tasks) == []
    assert spawned == []


@pytest.mark.parametrize("background", [False, True])
def test_a_read_only_ws_run_command_returns_the_error(
    spawned, isolated_tasks, tmp_path, monkeypatch, background
):
    from server.workspace import executors

    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(tmp_path))
    out = executors._exec_ws_run_command({"command": "ls", "run_in_background": background}, [], [])
    assert out == f"Error: {SANDBOX_UNAVAILABLE_MESSAGE}"
    assert "[Background Task Started]" not in out
    assert spawned == []


@pytest.mark.parametrize("background", [False, True])
def test_an_approved_command_is_not_reported_as_ok(
    spawned, isolated_tasks, tmp_path, monkeypatch, background
):
    from server.approval.executors import _do_command

    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: str(tmp_path))
    outcome = asyncio.run(_do_command({"command": "touch x", "run_in_background": background}))
    assert outcome.ok is False
    assert outcome.error == SANDBOX_UNAVAILABLE_MESSAGE
    assert not (tmp_path / "x").exists()
    assert spawned == []


def test_run_python_returns_the_error(spawned, monkeypatch, tmp_path):
    from server.executors import code

    monkeypatch.setattr(code, "_AWS_DIR", str(tmp_path / ".aws"))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    assert code.do_run_python({"code": "print(1)"}) == (False, SANDBOX_UNAVAILABLE_MESSAGE)
    assert spawned == []


def test_aws_cli_returns_the_error(spawned, monkeypatch, tmp_path):
    from server.executors import code

    monkeypatch.setattr(code, "_AWS_DIR", str(tmp_path / ".aws"))
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    assert code.do_aws_cli({"command": "aws s3 ls"}) == (False, SANDBOX_UNAVAILABLE_MESSAGE)
    assert spawned == []


def test_office_script_returns_the_error(spawned):
    from server.documents.script import run_office_script

    ok, message, data = run_office_script("print(1)", "docx")
    assert ok is False and data == b""
    assert SANDBOX_UNAVAILABLE_MESSAGE in message
    assert spawned == []


# ── preview dev server ────────────────────────────────────────────────────


def test_a_dev_server_spawn_error_names_the_reason(spawned, tmp_path):
    from server.preview.process import DevServerProcess, DevServerSpawnError

    with pytest.raises(DevServerSpawnError) as exc:
        asyncio.run(DevServerProcess.spawn(["npm", "run", "dev"], cwd=str(tmp_path)))
    assert str(exc.value) == SANDBOX_UNAVAILABLE_MESSAGE
    assert spawned == []


# ── hooks ─────────────────────────────────────────────────────────────────


def test_the_hook_test_button_shows_the_reason(spawned, tmp_path):
    from server.hooks.engine import dry_run

    result = dry_run("true", {"event": "Stop"}, cwd=str(tmp_path))
    assert result["exit_code"] == -1
    assert SANDBOX_UNAVAILABLE_MESSAGE in result["stderr"]
    assert spawned == []


@pytest.mark.parametrize("on_error", ["block", "ignore"])
def test_a_hook_that_cannot_run_is_a_hook_failure(spawned, tmp_path, monkeypatch, on_error):
    from server.hooks import engine
    from server.hooks.schema import HookDef

    async def no_inprocess(event, payload):
        return []

    hook = HookDef(event="PreToolUse", command="true", on_error=on_error, id="h_fc").clamp()
    monkeypatch.setattr("server.infrastructure.plugin_hooks.run_inprocess", no_inprocess)
    monkeypatch.setattr(engine, "merged_for_event", lambda event, ws: [hook])
    warnings = []
    monkeypatch.setattr(engine.log, "warning", lambda fmt, *a: warnings.append(fmt % a))
    outcome = asyncio.run(
        engine.run_hooks("PreToolUse", {"event": "PreToolUse"}, workspace=str(tmp_path))
    )
    if on_error == "block":
        assert outcome.blocked
        assert SANDBOX_UNAVAILABLE_MESSAGE in outcome.reason
        assert not any("skipped" in w for w in warnings)
    else:
        assert not outcome.blocked
        # Skipped, but never silently: the log says so, with the reason.
        assert any(
            "could not run and was skipped" in w and SANDBOX_UNAVAILABLE_MESSAGE in w
            for w in warnings
        )
    assert spawned == []
