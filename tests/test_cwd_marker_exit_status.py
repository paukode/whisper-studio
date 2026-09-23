"""The cwd marker appended after every workspace command must not replace the
command's exit status: a failing command has to report failure, with its
output intact, instead of pwd's 0."""

import asyncio
import os
import subprocess

from server.cwd_tracker import extract_cwd_from_output, wrap_command_for_cwd


def _run_wrapped(command: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", "-c", wrap_command_for_cwd(command)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_failing_command_keeps_its_status_and_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    result = _run_wrapped("cd sub && sh -c 'exit 3'", str(tmp_path))
    assert result.returncode == 3
    _, cwd = extract_cwd_from_output(result.stdout.strip())
    assert os.path.realpath(cwd) == os.path.realpath(tmp_path / "sub")


def test_succeeding_command_still_reports_zero(tmp_path):
    result = _run_wrapped("echo RAN", str(tmp_path))
    assert result.returncode == 0
    clean, cwd = extract_cwd_from_output(result.stdout.strip())
    assert clean == "RAN"
    assert os.path.realpath(cwd) == os.path.realpath(tmp_path)


def test_approved_command_failure_reaches_the_model_with_its_output(tmp_path, monkeypatch):
    from server import workspace
    from server.approval.bootstrap import _do_command, register_defaults
    from server.tool_executor import _execute_ws_approval_inline

    register_defaults()  # idempotent; registers the "command" ApprovalSpec
    monkeypatch.setattr(workspace, "get_workspace_path", lambda: str(tmp_path))
    command = "echo partial-output; ls no-such-file"

    outcome = asyncio.run(_do_command({"command": command}))
    assert outcome.ok is False
    assert outcome.error.startswith("exit code ")
    assert "partial-output" in outcome.output

    text = asyncio.run(_execute_ws_approval_inline({"action": "command", "command": command}))
    assert text.startswith("Error: exit code ")
    assert "partial-output" in text
