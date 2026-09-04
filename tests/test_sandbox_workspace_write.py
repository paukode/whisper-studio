"""Codex-style default-deny writes: write_mode="workspace".

The macOS profile was (allow default) plus a secret deny-list, so any approved
or agent-issued command could write anywhere outside the listed secrets. A
denylist protects the paths someone thought of; default-deny protects the
rest. In workspace mode, writes are confined to the working tree (plus the
git dirs a linked worktree needs), system temp, and explicit allow_paths,
with the secret denies LAST so a workspace containing the home directory can
never re-expose ~/.ssh. Agent-stamped (unattended) commands get workspace
mode; human-approved commands keep the open mode (the approval card is the
control there). Linux/bwrap already mounted the root read-only, so this is
macOS catching up.
"""

import asyncio
import os
import platform
import shutil
import tempfile

import pytest

from server import sandbox


def test_open_mode_profile_is_unchanged():
    profile = sandbox._generate_macos_profile("/tmp")
    assert "(deny file-write*)" not in profile
    assert "(allow file-write*" not in profile


def test_workspace_mode_denies_by_default_and_allows_the_roots(tmp_path):
    profile = sandbox._generate_macos_profile(str(tmp_path), write_mode="workspace")
    assert "(deny file-write*)" in profile
    assert f'(allow file-write* (subpath "{os.path.realpath(str(tmp_path))}"))' in profile
    assert '(allow file-write* (subpath "/private/tmp"))' in profile


def test_workspace_mode_rule_order_is_deny_allow_secrets(tmp_path):
    """SBPL: a later rule overrides an earlier match, so the secret denies must
    come after the write allows or a home-dir workspace re-exposes ~/.ssh."""
    profile = sandbox._generate_macos_profile(str(tmp_path), write_mode="workspace")
    global_deny = profile.index("(deny file-write*)")
    first_allow = profile.index("(allow file-write*")
    first_secret_deny = profile.index("(deny file-read* file-write*")
    assert global_deny < first_allow < first_secret_deny


def test_allow_paths_are_writable_in_workspace_mode(tmp_path):
    profile = sandbox._generate_macos_profile(
        str(tmp_path), allow_paths=["~/.aws"], write_mode="workspace"
    )
    aws = os.path.realpath(os.path.expanduser("~/.aws"))
    assert f'(allow file-write* (subpath "{aws}"))' in profile


def test_git_write_roots_resolves_worktree_and_common_dirs(tmp_path):
    main_git = tmp_path / "main" / ".git"
    wt_gitdir = main_git / "worktrees" / "agent-x"
    wt_gitdir.mkdir(parents=True)
    (wt_gitdir / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_gitdir}\n")

    roots = sandbox._git_write_roots(str(wt))
    assert os.path.realpath(str(wt_gitdir)) in roots
    assert os.path.realpath(str(main_git)) in roots


def test_plain_checkout_has_no_extra_git_roots(tmp_path):
    (tmp_path / ".git").mkdir()  # a directory, not a linked-worktree file
    assert sandbox._git_write_roots(str(tmp_path)) == []


def test_agent_stamped_terminal_run_uses_workspace_write(monkeypatch):
    from server.executors.terminal_run import do_terminal_run

    seen = {}

    async def fake_run_in_sandbox(command, cwd=None, timeout=None, write_mode="open"):
        seen["write_mode"] = write_mode
        return {"exit_code": 0, "output": "ok", "timed_out": False}

    monkeypatch.setattr("server.terminal.run_in_sandbox", fake_run_in_sandbox)

    ok, _ = asyncio.run(do_terminal_run({"command": "echo hi", "__agent__": True}))
    assert ok
    assert seen["write_mode"] == "workspace"

    ok, _ = asyncio.run(do_terminal_run({"command": "echo hi"}))
    assert ok
    assert seen["write_mode"] == "open"


@pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)
def test_workspace_mode_blocks_writes_outside_the_tree_for_real():
    """End-to-end kernel check: in workspace mode a write outside the working
    tree fails and one inside succeeds; open mode still writes anywhere."""
    home_probe = tempfile.mkdtemp(prefix="whisper-sbx-probe-", dir=os.path.expanduser("~"))
    ws = tempfile.mkdtemp(prefix="whisper-sbx-ws-")
    try:
        blocked = sandbox.run_sandboxed(
            f"touch {home_probe}/f", cwd=ws, timeout=20, write_mode="workspace"
        )
        assert blocked.returncode != 0
        assert not os.path.exists(f"{home_probe}/f")

        inside = sandbox.run_sandboxed(f"touch {ws}/f", cwd=ws, timeout=20, write_mode="workspace")
        assert inside.returncode == 0
        assert os.path.exists(f"{ws}/f")

        open_mode = sandbox.run_sandboxed(
            f"touch {home_probe}/g", cwd=ws, timeout=20, write_mode="open"
        )
        assert open_mode.returncode == 0
        assert os.path.exists(f"{home_probe}/g")
    finally:
        shutil.rmtree(home_probe, ignore_errors=True)
        shutil.rmtree(ws, ignore_errors=True)
