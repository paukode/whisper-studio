"""A session's own scratch folder inside the shell sandbox.

The field report (2026-10-22): in bypass mode the model kept raising "FULL
FILESYSTEM ACCESS" cards. The prompt sends throwaway files, virtualenvs and
test caches to the session's scratch folder, but the workspace-write sandbox
let a command write only in its working tree and temp, so every scratch write
failed and the model escalated to danger-full-access, which always asks.

Now a confined run may also write the calling session's own scratch folder
(never another session's), and npm, pip and uv keep their package caches in
it, since their usual home-folder caches are outside the confinement. Every
entry point that confines a session's command passes the folder:
ws_run_command (inline, approved, handed off, background), terminal_run,
terminal_send and run_python.
"""

import asyncio
import json
import os
import platform
import shutil
import tempfile

import pytest

from server import sandbox
from server.workspace import paths
from server.workspace.paths import own_scratch_dir

SID = "sess-1"

needs_sandbox_exec = pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


# ── the profile and the environment ───────────────────────────────────────


def test_a_confined_profile_allows_the_scratch_folder(tmp_path):
    ws = tmp_path / "ws"
    scratch = tmp_path / "scratch" / SID
    ws.mkdir()
    scratch.mkdir(parents=True)
    rule = f'(allow file-write* (subpath "{os.path.realpath(scratch)}"))'
    assert rule in sandbox._generate_macos_profile(
        str(ws), write_mode="workspace", scratch_dir=str(scratch)
    )
    assert rule not in sandbox._generate_macos_profile(str(ws), write_mode="workspace")
    readonly = sandbox._generate_macos_profile(
        str(ws), write_mode="readonly", scratch_dir=str(scratch)
    )
    assert str(scratch) not in readonly


def test_package_caches_live_in_the_scratch_folder_of_a_confined_run(tmp_path):
    scratch = str(tmp_path / SID)
    env = sandbox._scratch_env("workspace", scratch)
    assert set(env) == {"npm_config_cache", "PIP_CACHE_DIR", "UV_CACHE_DIR"}
    for value in env.values():
        assert value.startswith(os.path.join(scratch, "cache") + os.sep)
    # An open run may use the user's own caches; a read-only one writes none.
    assert sandbox._scratch_env("open", scratch) == {}
    assert sandbox._scratch_env("readonly", scratch) == {}
    assert sandbox._scratch_env("workspace", None) == {}


def test_bwrap_binds_the_scratch_folder_except_read_only(tmp_path):
    ws, scratch = str(tmp_path / "ws"), str(tmp_path / SID)
    argv = sandbox._bwrap_argv("ls", ws, None, "workspace", scratch_dir=scratch)
    head = argv[: argv.index("--chdir")]
    assert any(head[i : i + 3] == ["--bind", scratch, scratch] for i in range(len(head)))
    assert scratch not in sandbox._bwrap_argv("ls", ws, None, "readonly", scratch_dir=scratch)


def test_popen_sandboxed_hands_the_cache_env_to_a_confined_child(tmp_path, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen["env"] = kw["env"]
            seen["argv"] = argv

    monkeypatch.setattr(sandbox.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sandbox, "_is_sandbox_exec_available", lambda: False)
    monkeypatch.setattr(sandbox, "_is_bwrap_available", lambda: False)
    scratch = str(tmp_path / SID)
    with open(os.devnull, "w") as out:
        sandbox.popen_sandboxed(
            "true", cwd=str(tmp_path), stdout_file=out, write_mode="workspace", scratch_dir=scratch
        )
    assert seen["env"]["npm_config_cache"] == os.path.join(scratch, "cache", "npm")


# ── the kernel agrees ─────────────────────────────────────────────────────


@needs_sandbox_exec
def test_a_confined_command_writes_its_own_scratch_but_not_another():
    """Under the home folder, not temp: temp is writable to every confined
    run, so a probe there would pass whatever the profile says."""
    root = tempfile.mkdtemp(prefix="whisper-scratch-probe-", dir=os.path.expanduser("~"))
    ws = tempfile.mkdtemp(prefix="whisper-sbx-ws-")
    own = os.path.join(root, SID)
    other = os.path.join(root, "other-session")
    os.makedirs(own)
    os.makedirs(other)
    try:
        ok = sandbox.run_sandboxed(
            f'mkdir -p {own}/venv && touch {own}/venv/f && printf %s "$npm_config_cache"',
            cwd=ws,
            timeout=20,
            write_mode="workspace",
            scratch_dir=own,
        )
        assert ok.returncode == 0, ok.stderr
        assert os.path.exists(f"{own}/venv/f")
        assert ok.stdout == os.path.join(own, "cache", "npm")

        blocked = sandbox.run_sandboxed(
            f"touch {other}/f", cwd=ws, timeout=20, write_mode="workspace", scratch_dir=own
        )
        assert blocked.returncode != 0
        assert not os.path.exists(f"{other}/f")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(ws, ignore_errors=True)


@needs_sandbox_exec
def test_terminal_run_writes_its_sessions_scratch_without_escalation(monkeypatch):
    """The field case end to end: a default terminal_run (workspace-write, no
    danger-full-access) creates and removes files in its scratch folder."""
    from server.executors.terminal_run import do_terminal_run

    root = tempfile.mkdtemp(prefix="whisper-scratch-probe-", dir=os.path.expanduser("~"))
    ws = tempfile.mkdtemp(prefix="whisper-sbx-ws-")
    monkeypatch.setattr(paths, "DATA_DIR", root)
    try:
        scratch = own_scratch_dir(SID)
        command = (
            f"mkdir -p {scratch}/probe && touch {scratch}/probe/f && rm -rf {scratch}/probe "
            '&& echo "cache=$PIP_CACHE_DIR"'
        )
        ok, out = asyncio.run(
            do_terminal_run({"command": command, "cwd": ws, "session_id": SID, "timeout": 20})
        )
        assert ok, out
        assert "exit_code: 0" in out, out
        assert f"cache={os.path.join(scratch, 'cache', 'pip')}" in out
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(ws, ignore_errors=True)


# ── every entry point passes its session's folder ─────────────────────────


def test_terminal_run_cards_carry_the_session(data_dir):
    from server.executors.terminal_run import _exec_terminal_run

    out = _exec_terminal_run({"command": "ls", "__session_id__": SID}, [], [])
    assert json.loads(out[len("[WS_APPROVAL]") :])["session_id"] == SID


def test_terminal_run_and_send_confine_to_their_sessions_scratch(data_dir, monkeypatch):
    from server.executors.terminal_run import do_terminal_run, do_terminal_send

    seen = {}

    async def fake_run_in_sandbox(command, *, scratch_dir=None, **_):
        seen["run"] = scratch_dir
        return {"exit_code": 0, "output": "ok", "timed_out": False}

    def fake_open(chat_session_id, cwd, write_mode="workspace", scratch_dir=None, **_):
        seen["send"] = scratch_dir
        return object()

    async def fake_send(session, text, timeout):
        return {"wait_reason": "inferred_idle", "output": "ok"}

    monkeypatch.setattr("server.terminal.run_in_sandbox", fake_run_in_sandbox)
    monkeypatch.setattr("server.terminal.get_or_open_interactive", fake_open)
    monkeypatch.setattr("server.terminal.send_and_wait", fake_send)
    monkeypatch.setattr(sandbox, "pty_sandbox_backend", lambda: "sandbox-exec")

    assert asyncio.run(do_terminal_run({"command": "ls", "session_id": SID}))[0]
    assert seen["run"] == own_scratch_dir(SID)
    assert asyncio.run(do_terminal_send({"input": "1+1", "session_id": SID}))[0]
    assert seen["send"] == own_scratch_dir(SID)


def test_run_python_confines_to_its_sessions_scratch(data_dir, monkeypatch):
    from server.executors import code

    out = code.exec_run_python({"code": "print(1)", "__session_id__": SID}, [], [])
    payload = json.loads(out[len("[WS_APPROVAL]") :])
    assert payload["session_id"] == SID

    seen = {}

    def fake_run_sandboxed(command, **kw):
        seen.update(kw)
        return sandbox.subprocess.CompletedProcess(command, 0, "1\n", "")

    monkeypatch.setattr(code, "run_sandboxed", fake_run_sandboxed)
    assert code.do_run_python(payload)[0]
    assert seen["write_mode"] == "workspace"
    assert seen["scratch_dir"] == own_scratch_dir(SID)


def test_ws_run_command_confines_to_its_sessions_scratch(data_dir, monkeypatch, tmp_path):
    from server.tasks import handoff
    from server.workspace import executors

    seen = {}

    def fake_handoff(command, exec_command, **kw):
        seen["handoff"] = kw["scratch_dir"]
        return handoff.HandoffResult(background=False, output="", returncode=0)

    def fake_task(command, **kw):
        seen["task"] = kw["scratch_dir"]
        seen["task_root"] = kw["write_root"]
        return {"task_id": "t1", "output_path": "/dev/null", "status": "running"}

    monkeypatch.setattr("server.tasks.handoff.run_with_handoff", fake_handoff)
    monkeypatch.setattr("server.tasks.shell.start_shell_task", fake_task)
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    executors.run_workspace_command("ls", ws, session_id=SID, write_mode="workspace")
    executors.run_workspace_command(
        "ls", ws, session_id=SID, write_mode="workspace", run_in_background=True
    )
    assert seen["handoff"] == seen["task"] == own_scratch_dir(SID)
    # A background run is confined to the workspace, like a foreground one.
    assert seen["task_root"] == ws


# ── the prompt names the same folder ──────────────────────────────────────


def test_the_prompt_names_the_sessions_folder_and_its_cache(data_dir):
    from server.prompts.workspace import workspace_prompt

    text = workspace_prompt("/ws", SID)
    scratch = own_scratch_dir(SID)
    cache = os.path.dirname(sandbox.scratch_cache_env(scratch)["npm_config_cache"])
    assert f"'{scratch}'" in text
    assert f"'{cache}'" in text
    assert "virtualenv in the scratch folder" in text
    assert "—" not in text
