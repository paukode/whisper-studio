"""Pins for the shell-profile snapshot wrapper: the snapshot is shell-specific
(zsh-only syntax is a fatal error under /bin/sh), and every executor runs
command strings with /bin/sh — run_sandboxed and the background shell runner alike — so
wrap_command must re-execute under the shell the snapshot was captured for."""

import os
import shutil
import subprocess

import pytest

import server.shell_snapshot as shell_snapshot

ZSH = shutil.which("zsh")

# Mirrors the p10k line in a real ~/.zshrc: ${(%):-%n} is a zsh prompt
# expansion that /bin/sh rejects as a bad substitution, aborting the whole
# script before the wrapped command runs.
ZSH_ONLY_SNAPSHOT = "echo ${(%):-%n} > /dev/null 2>&1\n"


def _run_like_executor(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.skipif(not ZSH, reason="zsh not installed")
def test_zsh_snapshot_survives_sh_executor(monkeypatch):
    monkeypatch.setattr(shell_snapshot, "get_snapshot", lambda session_id: ZSH_ONLY_SNAPSHOT)
    monkeypatch.setattr(shell_snapshot, "_detect_shell", lambda: ZSH)
    wrapped = shell_snapshot.wrap_command("echo RAN", "s1")
    result = _run_like_executor(wrapped)
    assert result.returncode == 0, result.stderr
    assert "RAN" in result.stdout


@pytest.mark.skipif(not ZSH, reason="zsh not installed")
def test_cwd_marker_composes_with_snapshot_wrap(monkeypatch):
    # The snapshot wrap execs another shell, so it must be the outermost
    # wrapper: a cwd marker appended inside must still reach stdout, and the
    # cd must be visible to the pwd that follows it.
    from server.cwd_tracker import extract_cwd_from_output, wrap_command_for_cwd

    monkeypatch.setattr(shell_snapshot, "get_snapshot", lambda session_id: ZSH_ONLY_SNAPSHOT)
    monkeypatch.setattr(shell_snapshot, "_detect_shell", lambda: ZSH)
    wrapped = shell_snapshot.wrap_command(wrap_command_for_cwd("cd /tmp && echo RAN"), "s1")
    result = _run_like_executor(wrapped)
    assert result.returncode == 0, result.stderr
    clean, cwd = extract_cwd_from_output(result.stdout.strip())
    assert "RAN" in clean
    assert os.path.realpath(cwd) == os.path.realpath("/tmp")


def test_no_snapshot_passes_command_through(monkeypatch):
    monkeypatch.setattr(shell_snapshot, "get_snapshot", lambda session_id: None)
    assert shell_snapshot.wrap_command("echo hi", "s1") == "echo hi"
