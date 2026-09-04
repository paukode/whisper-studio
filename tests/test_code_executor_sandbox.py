"""do_aws_cli and do_run_python must run under the OS sandbox (with ~/.aws
allowed so AWS auth still works), and aws_cli must validate the command."""

import os
import subprocess

from server.executors import code as codemod


def test_aws_cli_rejects_chained_dangerous_command():
    ok, msg = codemod.do_aws_cli({"command": "aws s3 ls; rm -rf /tmp/x"})
    assert ok is False
    assert msg


def test_aws_cli_runs_sandboxed_with_aws_allowed(monkeypatch):
    calls = {}

    def fake(cmd, *, cwd, timeout, allow_paths=None, write_mode="open"):
        calls["cmd"] = cmd
        calls["allow_paths"] = allow_paths
        calls["write_mode"] = write_mode
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(codemod, "run_sandboxed", fake)
    ok, out = codemod.do_aws_cli({"command": "aws s3 ls"})
    assert ok is True
    assert calls["allow_paths"] and any(".aws" in p for p in calls["allow_paths"])
    assert "aws s3 ls" in calls["cmd"]
    # Human-approved runs keep the open write mode; agent-stamped runs are
    # write-confined (workspace mode).
    assert calls["write_mode"] == "open"
    ok, out = codemod.do_aws_cli({"command": "aws s3 ls", "__agent__": True})
    assert ok is True
    assert calls["write_mode"] == "workspace"


def test_run_python_runs_sandboxed_and_cleans_temp(monkeypatch):
    captured = {}

    def fake(cmd, *, cwd, timeout, allow_paths=None, write_mode="open"):
        captured["cmd"] = cmd
        captured["allow_paths"] = allow_paths
        captured["write_mode"] = write_mode
        return subprocess.CompletedProcess(cmd, 0, "hi", "")

    monkeypatch.setattr(codemod, "run_sandboxed", fake)
    ok, out = codemod.do_run_python({"code": "print('hi')"})
    assert ok is True
    assert "python3" in captured["cmd"]
    assert captured["allow_paths"] and any(".aws" in p for p in captured["allow_paths"])
    assert captured["write_mode"] == "open"

    ok, out = codemod.do_run_python({"code": "print('hi')", "__agent__": True})
    assert ok is True
    assert captured["write_mode"] == "workspace"

    # The temp script is unlinked in finally — extract its path and confirm.
    path = captured["cmd"].split("python3 ", 1)[1].rsplit(" < /dev/null", 1)[0].strip().strip("'\"")
    assert not os.path.exists(path), "temp script should be cleaned up"
