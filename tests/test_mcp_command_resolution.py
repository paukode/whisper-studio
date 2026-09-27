"""Missing MCP command -> actionable error (not a raw errno), and GUI PATH enrichment."""

import asyncio
import os

from server.infrastructure import binaries
from server.mcp import MCPManager


def test_resolve_command_bare_and_absolute(tmp_path):
    # Bare name resolves against the given PATH.
    exe = tmp_path / "mytool"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert MCPManager._resolve_command("mytool", str(tmp_path)) == str(exe)
    assert MCPManager._resolve_command("nope-xyz", str(tmp_path)) is None
    # Absolute must exist + be executable.
    assert MCPManager._resolve_command(str(exe), None) == str(exe)
    assert MCPManager._resolve_command(str(tmp_path / "ghost"), None) is None


def test_start_server_missing_command_sets_friendly_error():
    mgr = MCPManager()
    asyncio.run(mgr.start_server("Broken", {"command": "definitely-not-a-real-binary-xyz"}))
    info = mgr._sessions.get("Broken")
    assert info is not None and info["status"] == "error"
    assert "was not found" in info["error"]
    assert "[Errno 2]" not in info["error"]


def test_login_env_noop_without_whisper_home(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    monkeypatch.setattr(binaries, "_LOGIN_ENV", {})
    before = os.environ.get("PATH", "")
    binaries.load_login_shell_env()
    assert os.environ.get("PATH", "") == before
    assert binaries.login_shell_env() == {}


def test_login_env_adds_common_dirs_when_capture_fails(monkeypatch, tmp_path):
    tooldir = tmp_path / "brew" / "bin"
    tooldir.mkdir(parents=True)
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(binaries, "_COMMON_TOOL_DIRS", (str(tooldir),))
    monkeypatch.setattr(binaries, "_LOGIN_ENV", {})
    # Force the login-shell capture to contribute nothing, exercising the fallback.
    monkeypatch.setattr(
        binaries.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no shell")),
    )
    binaries.load_login_shell_env()
    assert str(tooldir) in os.environ["PATH"].split(os.pathsep)
    assert binaries.login_shell_env() == {}


def _fake_shell_output(stdout: bytes):
    class _Done:
        pass

    done = _Done()
    done.stdout = stdout
    return lambda *a, **k: done


def test_login_env_keeps_the_full_environment_and_widens_path(monkeypatch, tmp_path):
    marker = binaries._ENV_MARKER.encode()
    body = b"\0".join(
        [
            b"AWS_PROFILE=work",
            b"MULTI=line one\nline two",
            b"PATH=/Users/me/.toolbox/bin:/usr/bin",
            b"PWD=/tmp/capture-shell",
            b"SHLVL=2",
            b"BASH_FUNC_x%%=() {  echo; }",
        ]
    )
    # An rc file that prints a banner before and after must not leak into
    # the parsed environment.
    stdout = b"Welcome banner\n" + marker + body + b"\0" + marker + b"bye\n"
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(binaries, "_COMMON_TOOL_DIRS", ())
    monkeypatch.setattr(binaries, "_LOGIN_ENV", {})
    monkeypatch.setattr(binaries.subprocess, "run", _fake_shell_output(stdout))

    binaries.load_login_shell_env()

    env = binaries.login_shell_env()
    assert env["AWS_PROFILE"] == "work"
    assert env["MULTI"] == "line one\nline two"
    # The capture shell's own bookkeeping and non-identifier keys are dropped.
    assert "PWD" not in env and "SHLVL" not in env
    assert not any(k.startswith("BASH_FUNC") for k in env)
    assert os.environ["PATH"].split(os.pathsep)[0] == "/Users/me/.toolbox/bin"
