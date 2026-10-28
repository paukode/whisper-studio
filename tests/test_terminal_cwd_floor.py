"""Where a terminal command runs, and what it may change there.

The user's contract (docs/tut-permissions.html, "The guards that never turn
off"): bypass may change anything inside the connected workspace and the
app's folders; a change outside must always ask a person; fail toward asking.

Three holes this pins shut:

1. terminal_run mode="visible" types into the user's own shell. That shell
   runs in whatever folder an earlier call (`cd ~/Documents`) or the user left
   it, ignores the call's cwd, and has no OS write confinement. The rm floor
   used to resolve a relative delete against the workspace, so `rm -rf notes`
   ran in bypass with no card. Now a visible run has no known working
   directory (every relative delete is outside), and every visible run asks a
   person in every mode but Bypass (the visible-terminal floor; the user's
   rule 2026-10-28: in Bypass the terminal opens and the command runs).
2. A sandbox run's relative cwd was resolved against the server process's own
   folder, while the floor joined it to the workspace. Now one resolver
   (terminal_run.resolve_cwd) feeds both.
3. A confined sandbox shell could write wherever it started (its write root
   was its cwd). Now it writes only in the workspace, its scratch folder and
   temp, wherever it starts; with no workspace, only the scratch folder and
   temp.
"""

import asyncio
import os
import platform
import shutil
import tempfile

import pytest

from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    approval_floor,
    resolve_static_decision,
)

ALL_MODES = (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_DONT_ASK, MODE_BYPASS)

needs_sandbox_exec = pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "project"
    (ws / "web").mkdir(parents=True)
    for target in (
        "server.workspace.state.get_workspace_path",
        "server.workspace.get_workspace_path",
    ):
        monkeypatch.setattr(target, lambda: str(ws))
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"mode": MODE_DEFAULT, "rules": [], "category_modes": {}},
    )
    return ws


def _run_payload(command, **extra):
    return {"action": "terminal_run", "command": command, "cwd": "", **extra}


def _decisions(payload, tool="terminal_run"):
    """The decision in every mode, under a session "Yes, all cli", and for a
    trusted skill script."""
    decide = lambda **kw: resolve_static_decision(tool, payload, "cli", **kw)  # noqa: E731
    out = [decide(session_approvals={}, mode=mode) for mode in ALL_MODES]
    out.append(decide(session_approvals={"cli": "allow"}, mode=MODE_BYPASS))
    out.append(decide(session_approvals={}, mode=MODE_DEFAULT, auto_allow_trusted=True))
    return out


# ── 1. The visible terminal ────────────────────────────────────────────────


def test_the_field_report_a_relative_delete_after_cd_asks_in_bypass(workspace):
    # Call 1 `cd ~/Documents` moved the visible shell; call 2 deletes there.
    payload = _run_payload("rm -rf notes", mode="visible")
    assert approval_floor("terminal_run", payload, "cli") == "rm"
    assert set(_decisions(payload)) == {"ask"}

    # The one-call form: the shell is still in the workspace when the call
    # arrives, so only "no known working directory" catches it.
    payload = _run_payload("cd ~/Documents && rm -rf notes", mode="visible")
    assert approval_floor("terminal_run", payload, "cli") == "rm"


def test_a_visible_run_asks_in_every_mode_but_bypass(workspace):
    """Outside Bypass a person approves every visible run, and a "Yes, all
    cli" or a trusted skill never stands in for them. In Bypass it runs."""
    for command in ("npm test", "ls", "touch ~/x", f"rm -rf {workspace / 'web'}"):
        payload = _run_payload(command, mode="visible")
        for mode in (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_DONT_ASK):
            assert approval_floor("terminal_run", payload, "cli", mode) is not None, command
            assert resolve_static_decision("terminal_run", payload, "cli", {}, mode) == "ask"

        def decide(**kw):
            return resolve_static_decision("terminal_run", payload, "cli", **kw)  # noqa: B023

        assert decide(session_approvals={"cli": "allow"}, mode=MODE_DEFAULT) == "ask", command
        assert decide(session_approvals={}, mode=MODE_DEFAULT, auto_allow_trusted=True) == "ask"
        assert approval_floor("terminal_run", payload, "cli", MODE_BYPASS) is None, command
        assert decide(session_approvals={}, mode=MODE_BYPASS) == "allow", command
    # An absolute delete inside the workspace is inside for the rm floor; the
    # visible-terminal floor is what asks for it outside Bypass.
    inside = _run_payload(f"rm -rf {workspace / 'web'}", mode=" visible ")
    assert approval_floor("terminal_run", inside, "cli") == "visible-terminal"


def test_a_visible_delete_asks_even_in_bypass(workspace):
    # The visible shell's folder is unknown, so a relative delete is outside
    # the workspace for the rm floor, which no mode lifts.
    payload = _run_payload("rm -rf dist", mode="visible")
    assert approval_floor("terminal_run", payload, "cli", MODE_BYPASS) == "rm"
    assert resolve_static_decision("terminal_run", payload, "cli", {}, MODE_BYPASS) == "ask"


def test_a_sandbox_run_still_follows_the_mode(workspace):
    payload = _run_payload("npm test")
    assert approval_floor("terminal_run", payload, "cli") is None
    assert resolve_static_decision("terminal_run", payload, "cli", {}, MODE_BYPASS) == "allow"
    payload = _run_payload("rm -rf web/dist")
    assert approval_floor("terminal_run", payload, "cli") is None


def test_an_unattended_agent_cannot_type_into_the_visible_terminal():
    from server.executors.terminal_run import do_terminal_run

    ok, msg = asyncio.run(do_terminal_run({"command": "ls", "mode": "visible", "__agent__": True}))
    assert not ok
    assert "unattended" in msg


# ── 2. One working directory for the floor and the run ─────────────────────


def _capture_sandbox_run(monkeypatch):
    from server import sandbox

    seen = {}

    async def fake_run_in_sandbox(command, cwd=None, timeout=None, write_mode="open", **kw):
        seen.update(cwd=cwd, write_mode=write_mode, write_root=kw.get("write_root"))
        return {"exit_code": 0, "output": "ok", "timed_out": False}

    monkeypatch.setattr("server.terminal.run_in_sandbox", fake_run_in_sandbox)
    monkeypatch.setattr(sandbox, "pty_sandbox_backend", lambda: "sandbox-exec")
    return seen


def test_a_relative_cwd_is_the_workspaces_not_the_servers(workspace, tmp_path, monkeypatch):
    from server.executors.terminal_run import do_terminal_run

    # The server process's own folder also has a "web" folder (the packaged
    # backend runs from inside the app bundle, a source install from the repo).
    server_dir = tmp_path / "server_process"
    (server_dir / "web").mkdir(parents=True)
    monkeypatch.chdir(server_dir)
    seen = _capture_sandbox_run(monkeypatch)

    assert asyncio.run(do_terminal_run({"command": "ls", "cwd": "web"}))[0]
    assert seen["cwd"] == str(workspace / "web")
    assert approval_floor("terminal_run", _run_payload("rm -rf build", cwd="web"), "cli") is None


def test_a_missing_cwd_falls_back_for_the_floor_too(workspace, monkeypatch):
    from server.executors.terminal_run import do_terminal_run

    seen = _capture_sandbox_run(monkeypatch)
    assert asyncio.run(do_terminal_run({"command": "ls", "cwd": "missing"}))[0]
    assert seen["cwd"] == str(workspace)
    # The run starts in the workspace, so ../notes is its parent's: outside.
    # Read against the missing folder it would have looked inside.
    payload = _run_payload("rm -rf ../notes", cwd="missing")
    assert approval_floor("terminal_run", payload, "cli") == "rm"


def test_with_no_workspace_a_relative_cwd_starts_at_home(monkeypatch):
    from server.executors.terminal_run import resolve_cwd

    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    assert resolve_cwd("web", None) == os.path.expanduser("~")
    assert approval_floor("terminal_run", _run_payload("rm -rf build", cwd="web"), "cli") == "rm"


# ── 3. A confined shell writes only in the workspace ───────────────────────


def test_a_sandbox_run_started_elsewhere_is_confined_to_the_workspace(
    workspace, tmp_path, monkeypatch
):
    from server.executors.terminal_run import do_terminal_run

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    seen = _capture_sandbox_run(monkeypatch)
    assert asyncio.run(do_terminal_run({"command": "ls", "cwd": str(elsewhere)}))[0]
    assert seen == {"cwd": str(elsewhere), "write_mode": "workspace", "write_root": str(workspace)}

    for target in (
        "server.workspace.state.get_workspace_path",
        "server.workspace.get_workspace_path",
    ):
        monkeypatch.setattr(target, lambda: None)
    assert asyncio.run(do_terminal_run({"command": "ls"}))[0]
    assert seen["write_root"] is None


def test_terminal_send_is_confined_to_the_workspace(workspace, tmp_path, monkeypatch):
    from server.executors.terminal_run import do_terminal_send

    seen = {}

    def fake_open(chat_session_id, cwd, write_mode="workspace", scratch_dir=None, write_root=None):
        seen.update(cwd=cwd, write_mode=write_mode, write_root=write_root)
        return object()

    async def fake_send(session, text, timeout):
        return {"wait_reason": "inferred_idle", "output": "ok"}

    monkeypatch.setattr("server.terminal.get_or_open_interactive", fake_open)
    monkeypatch.setattr("server.terminal.send_and_wait", fake_send)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = {"input": "1+1", "session_id": "s1", "cwd": str(elsewhere)}
    assert asyncio.run(do_terminal_send(payload))[0]
    assert seen == {"cwd": str(elsewhere), "write_mode": "workspace", "write_root": str(workspace)}


def test_the_interactive_terminal_reopens_for_a_new_workspace(monkeypatch):
    from server import terminal

    opened = []

    class FakeSession:
        def __init__(self, write_root):
            self.id = f"pty{len(opened)}"
            self.write_root = write_root
            self.alive = True

        def kill(self):
            self.alive = False

    def fake_create(cwd, *, hidden, write_mode, scratch_dir, write_root):
        session = FakeSession(write_root)
        opened.append(session)
        with terminal._sessions_lock:
            terminal._sessions[session.id] = session
        return session

    monkeypatch.setattr(terminal, "_create_pty_session", fake_create)
    monkeypatch.setattr(terminal, "_interactive_ptys", {})
    first = terminal.get_or_open_interactive("chat", "/a", write_root="/a")
    assert terminal.get_or_open_interactive("chat", "/a", write_root="/a") is first
    second = terminal.get_or_open_interactive("chat", "/b", write_root="/b")
    assert second is not first and second.write_root == "/b"
    assert not first.alive
    with terminal._sessions_lock:
        assert first.id not in terminal._sessions
        terminal._sessions.pop(second.id, None)


def test_a_profile_with_no_workspace_writes_no_tree(tmp_path):
    from server import sandbox

    scratch = os.path.realpath(str(tmp_path / "scratch"))
    profile = sandbox._generate_macos_profile(None, write_mode="workspace", scratch_dir=scratch)
    allowed = [line for line in profile.splitlines() if line.startswith("(allow file-write*")]
    assert f'(allow file-write* (subpath "{scratch}"))' in allowed
    assert not any(os.path.expanduser("~") + '"' in line for line in allowed)
    assert all(
        any(f'"{root}"' in line for root in (scratch, *sandbox._WORKSPACE_WRITE_SYSTEM_ROOTS))
        for line in allowed
    )


@needs_sandbox_exec
def test_a_shell_started_outside_the_workspace_cannot_write_there_for_real():
    """End to end through sandbox-exec: the hidden PTY starts in a folder
    outside the workspace; a write where it started is refused, one in the
    workspace lands. Both folders are under the home folder, outside temp."""
    from server.terminal import run_in_sandbox

    home = os.path.expanduser("~")
    ws = tempfile.mkdtemp(prefix="whisper-tr-ws-", dir=home)
    elsewhere = tempfile.mkdtemp(prefix="whisper-tr-out-", dir=home)
    try:

        def run(command, write_root):
            return asyncio.run(
                run_in_sandbox(
                    command,
                    cwd=elsewhere,
                    timeout=20,
                    write_mode="workspace",
                    write_root=write_root,
                )
            )

        run(f"touch here; touch {ws}/there", ws)
        assert not os.path.exists(os.path.join(elsewhere, "here"))
        assert os.path.exists(os.path.join(ws, "there"))

        run("touch alone", None)
        assert not os.path.exists(os.path.join(elsewhere, "alone"))
    finally:
        shutil.rmtree(ws, ignore_errors=True)
        shutil.rmtree(elsewhere, ignore_errors=True)


# ── ws_run_command: the card and the floor read the tracked cwd ────────────


def test_a_ws_run_command_payload_names_the_tracked_cwd(workspace, tmp_path, monkeypatch):
    import json

    from server import cwd_tracker
    from server.workspace import executors

    elsewhere = tmp_path / "Documents"
    elsewhere.mkdir()
    monkeypatch.setattr(executors, "get_workspace_path", lambda: str(workspace))
    cwd_tracker.update_cwd("s-cwd", str(elsewhere))
    try:
        out = executors._exec_ws_run_command(
            {"command": "rm -rf notes", "__session_id__": "s-cwd"}, [], []
        )
        payload = json.loads(out[len("[WS_APPROVAL]") :])
        assert payload["cwd"] == str(elsewhere)
        assert approval_floor("ws_run_command", payload, "cli") == "rm"
    finally:
        cwd_tracker.clear_session("s-cwd")

    # A session that never moved names the workspace, and a delete there
    # follows the mode.
    out = executors._exec_ws_run_command({"command": "rm -rf web", "__session_id__": "s2"}, [], [])
    payload = json.loads(out[len("[WS_APPROVAL]") :])
    assert payload["cwd"] == str(workspace)
    assert approval_floor("ws_run_command", payload, "cli") is None
