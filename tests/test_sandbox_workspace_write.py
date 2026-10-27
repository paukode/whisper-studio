"""Codex-style default-deny writes: write_mode="workspace".

The macOS profile was (allow default) plus a secret deny-list, so any approved
or agent-issued command could write anywhere outside the listed secrets. A
denylist protects the paths someone thought of; default-deny protects the
rest. In workspace mode, writes are confined to the working tree (plus the
git dirs a linked worktree needs), system temp, and explicit allow_paths,
with the secret denies LAST so a workspace containing the home directory can
never re-expose ~/.ssh. EVERY sandboxed run is workspace-confined by default
(consent and confinement are independent knobs); the only way out is the
per-call sandbox_permissions='danger-full-access' escalation, which always
forces a real approval. Linux/bwrap already mounted the root read-only, so
this is macOS catching up.
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


def test_allow_paths_stay_read_only_and_write_paths_are_writable(tmp_path):
    """~/.aws is readable for the AWS tools, and only its caches are writable
    (tests/test_aws_cli_one_program.py has the why)."""
    aws = os.path.realpath(os.path.expanduser("~/.aws"))
    cache = os.path.join(aws, "cli", "cache")
    profile = sandbox._generate_macos_profile(
        str(tmp_path), allow_paths=["~/.aws"], write_mode="workspace", write_paths=[cache]
    )
    assert f'(deny file-read* file-write* (subpath "{aws}"))' not in profile
    assert f'(allow file-write* (subpath "{aws}"))' not in profile
    assert f'(deny file-write* (subpath "{aws}"))' in profile
    assert f'(allow file-write* (subpath "{cache}"))' in profile


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


def test_terminal_run_defaults_to_workspace_write(monkeypatch):
    """Every sandboxed run is workspace-confined — agent-stamped or human-
    approved. Only an explicit danger-full-access escalation runs open."""
    from server.executors.terminal_run import do_terminal_run

    seen = {}

    async def fake_run_in_sandbox(command, cwd=None, timeout=None, write_mode="open", **_):
        seen["write_mode"] = write_mode
        return {"exit_code": 0, "output": "ok", "timed_out": False}

    monkeypatch.setattr("server.terminal.run_in_sandbox", fake_run_in_sandbox)
    monkeypatch.setattr(sandbox, "pty_sandbox_backend", lambda: "sandbox-exec")

    ok, _ = asyncio.run(do_terminal_run({"command": "echo hi", "__agent__": True}))
    assert ok
    assert seen["write_mode"] == "workspace"

    ok, _ = asyncio.run(do_terminal_run({"command": "echo hi"}))
    assert ok
    assert seen["write_mode"] == "workspace"

    ok, _ = asyncio.run(
        do_terminal_run(
            {
                "command": "echo hi",
                "sandbox_permissions": "danger-full-access",
                "justification": "writes an installer outside the workspace",
            }
        )
    )
    assert ok
    assert seen["write_mode"] == "open"

    ok, _ = asyncio.run(do_terminal_run({"command": "echo hi", "sandbox_permissions": "read-only"}))
    assert ok
    assert seen["write_mode"] == "readonly"


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


# ── ws_run_command: consent and confinement are separate knobs ──────────────
# A command a person approved on its card runs with open writes; one a mode,
# a session answer, a rule, the classifier or an agent let through runs
# workspace-write confined, so nothing changes outside the workspace without
# a person seeing it (the user's rule). The confinement root is the workspace
# itself, not the tracked cwd, so a session that cd'd out of the workspace
# cannot write where it went.


def _capture_run(monkeypatch):
    from server.workspace import executors

    seen = {}

    def fake_run(
        command,
        ws,
        *,
        session_id="",
        run_in_background=False,
        write_mode="open",
        secret_reads=False,
    ):
        seen["write_mode"] = write_mode
        seen["secret_reads"] = secret_reads
        return executors.CommandRun(text="ok", returncode=0)

    monkeypatch.setattr("server.workspace.executors.run_workspace_command", fake_run)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: "/tmp")
    monkeypatch.setattr("server.workspace._validate_command", lambda c, secret_reads=False: None)
    return seen


def test_a_human_approved_command_runs_open(monkeypatch):
    from server.approval.executors import _do_command

    seen = _capture_run(monkeypatch)
    assert asyncio.run(_do_command({"command": "echo hi"})).ok
    assert seen["write_mode"] == "open"


def test_an_unreviewed_or_agent_command_runs_workspace_confined(monkeypatch):
    from server.approval.executors import _do_command
    from server.approval.spec import UNREVIEWED_KEY

    seen = _capture_run(monkeypatch)
    assert asyncio.run(_do_command({"command": "echo hi", UNREVIEWED_KEY: True})).ok
    assert seen["write_mode"] == "workspace"
    assert asyncio.run(_do_command({"command": "echo hi", "__agent__": True})).ok
    assert seen["write_mode"] == "workspace"


def test_the_inline_path_stamps_every_payload_unreviewed(monkeypatch):
    from server.approval import registry
    from server.approval.bootstrap import register_defaults
    from server.approval.spec import UNREVIEWED_KEY, ApprovalOutcome
    from server.tool_approvals import _execute_ws_approval_inline

    register_defaults()
    seen = {}

    async def spy(payload):
        seen.update(payload)
        return ApprovalOutcome(ok=True, output="ran")

    monkeypatch.setattr(registry.get("command"), "executor", spy)
    assert (
        asyncio.run(_execute_ws_approval_inline({"action": "command", "command": "ls"})).output
        == "ran"
    )
    assert seen[UNREVIEWED_KEY] is True and "__agent__" not in seen
    asyncio.run(_execute_ws_approval_inline({"action": "command", "command": "ls"}, agent=True))
    assert seen["__agent__"] is True


def test_a_cards_yes_drops_any_origin_stamp():
    from server.approval.spec import (
        UNREVIEWED_KEY,
        ApprovalOutcome,
        ApprovalSpec,
        execute_approved_by_human,
        reviewed_by_human,
    )

    seen = {}

    async def spy(payload):
        seen.update(payload)
        return ApprovalOutcome(ok=True)

    spec = ApprovalSpec(category="cli", preview="command", summary="x", executor=spy)
    payload = {"command": "ls", UNREVIEWED_KEY: True, "__agent__": True}
    assert asyncio.run(execute_approved_by_human(spec, payload)).ok
    assert reviewed_by_human(seen)
    assert UNREVIEWED_KEY not in seen and "__agent__" not in seen


def test_a_confined_ws_run_command_is_confined_to_the_workspace_not_the_cwd(monkeypatch, tmp_path):
    from server.tasks import handoff
    from server.workspace import executors

    ws = tmp_path / "ws"
    ws.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    seen = {}

    def fake_handoff(
        command,
        exec_command,
        *,
        cwd,
        session_id="",
        timeout=0,
        write_mode="open",
        write_root=None,
        scratch_dir=None,
        secret_reads=False,
    ):
        seen.update(cwd=cwd, write_mode=write_mode, write_root=write_root)
        return handoff.HandoffResult(background=False, output="", returncode=0)

    monkeypatch.setattr("server.tasks.handoff.run_with_handoff", fake_handoff)
    monkeypatch.setattr("server.cwd_tracker.get_cwd", lambda session_id, default: str(elsewhere))
    executors.run_workspace_command("touch x", str(ws), session_id="s1", write_mode="workspace")
    assert seen["cwd"] == str(elsewhere)
    assert seen["write_root"] == str(ws)
    assert seen["write_mode"] == "workspace"


def test_popen_profile_confines_to_the_write_root(tmp_path):
    ws = os.path.realpath(str(tmp_path / "ws"))
    elsewhere = os.path.realpath(str(tmp_path / "elsewhere"))
    os.makedirs(ws)
    os.makedirs(elsewhere)
    profile = sandbox._generate_macos_profile(ws, write_mode="workspace")
    assert f'(allow file-write* (subpath "{ws}"))' in profile
    assert elsewhere not in profile
    argv = sandbox._bwrap_argv("ls", elsewhere, None, "workspace", write_root=ws)
    assert argv[argv.index("--bind") + 1] == ws
    assert elsewhere not in argv[: argv.index("--chdir")]
