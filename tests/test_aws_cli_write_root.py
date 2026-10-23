"""aws_cli writes only in the workspace, or in no tree without one.

The aws_cli executor rooted its workspace-write confinement at its cwd, and
with no workspace connected that cwd is $HOME. So a run that bypass (or a
"yes to all", a rule, Auto) let through with no card could write anywhere in
the home folder: ``aws s3 cp s3://bucket/x ~/Documents/x``. The user's rule
is that a change outside the workspace asks a person, even in bypass.

Now the write root is the workspace, or no tree when none is connected. The
cwd stays $HOME for relative reads. The session's own scratch folder, temp
and the CLI's ~/.aws cache stay writable. run_sandboxed takes the write root
the way popen_sandboxed does, with None meaning "no tree"; leaving it out
still roots a run at its cwd, so every other caller is unchanged.
"""

import json
import os
import platform
import shutil
import subprocess
import tempfile

import pytest

from server import sandbox
from server.executors import code
from server.workspace import paths
from server.workspace.paths import own_scratch_dir

SID = "sess-aws"
HOME = os.path.expanduser("~")
AWS = os.path.realpath(os.path.expanduser("~/.aws"))

needs_sandbox_exec = pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def _capture_aws_run(monkeypatch, workspace):
    """Run do_aws_cli with run_sandboxed replaced by a spy."""
    seen = {}

    def spy(command, **kw):
        seen.update(kw, command=command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(code, "run_sandboxed", spy)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: workspace)
    return seen


# ── the executor ───────────────────────────────────────────────────────────


def test_no_workspace_writes_no_tree_but_keeps_home_as_the_cwd(monkeypatch, data_dir):
    seen = _capture_aws_run(monkeypatch, None)
    ok, _ = code.do_aws_cli({"command": "aws s3 cp s3://b/x x", "session_id": SID})
    assert ok
    assert seen["write_mode"] == "workspace"
    assert seen["write_root"] is None
    assert seen["cwd"] == HOME
    assert seen["scratch_dir"] == own_scratch_dir(SID)
    assert os.path.expanduser("~/.aws") in seen["allow_paths"]


def test_a_connected_workspace_is_the_write_root_and_the_cwd(monkeypatch, tmp_path):
    ws = str(tmp_path)
    seen = _capture_aws_run(monkeypatch, ws)
    assert code.do_aws_cli({"command": "aws s3 ls"})[0]
    assert seen["write_root"] == ws
    assert seen["cwd"] == ws
    assert seen["scratch_dir"] is None  # no session id, no scratch folder


def test_a_workspace_that_is_gone_counts_as_none(monkeypatch, tmp_path):
    seen = _capture_aws_run(monkeypatch, str(tmp_path / "removed"))
    assert code.do_aws_cli({"command": "aws s3 ls"})[0]
    assert seen["write_root"] is None
    assert seen["cwd"] == HOME


def test_the_card_payload_carries_the_session_id():
    from server.approval import registry
    from server.approval.bootstrap import register_defaults

    out = code.exec_aws_cli({"command": "aws s3 ls", "__session_id__": SID}, None, None)
    parsed = json.loads(out.removeprefix("[WS_APPROVAL]"))
    register_defaults()
    payload = registry.get("aws_cli").build_payload(parsed)
    assert payload["session_id"] == SID
    assert payload["command"] == "aws s3 ls"


def test_a_sandbox_denial_gets_the_hint_and_keeps_the_exit_code(monkeypatch):
    def denied(command, **kw):
        return subprocess.CompletedProcess(command, 1, "", "[Errno 1] Operation not permitted")

    monkeypatch.setattr(code, "run_sandboxed", denied)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "sandbox-exec")
    ok, output = code.do_aws_cli({"command": "aws s3 cp s3://b/x /Users/someone/x"})
    assert not ok
    # _do_aws_cli reads the exit code from the start of the text.
    assert output.startswith("(exit code 1)")
    assert "[sandbox]" in output and "danger-full-access" in output


def test_an_ordinary_failure_gets_no_hint(monkeypatch):
    def failed(command, **kw):
        return subprocess.CompletedProcess(command, 255, "", "An error occurred (NoSuchBucket)")

    monkeypatch.setattr(code, "run_sandboxed", failed)
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    monkeypatch.setattr(sandbox, "sandbox_backend", lambda: "sandbox-exec")
    ok, output = code.do_aws_cli({"command": "aws s3 ls s3://nope"})
    assert not ok and "[sandbox]" not in output


# ── run_sandboxed and the profiles ─────────────────────────────────────────


def test_a_profile_with_no_tree_keeps_scratch_temp_and_the_aws_cache(tmp_path):
    scratch = str(tmp_path / SID)
    cache = os.path.join(AWS, "cli", "cache")
    profile = sandbox._generate_macos_profile(
        None,
        allow_paths=["~/.aws"],
        write_mode="workspace",
        scratch_dir=scratch,
        write_paths=[cache],
    )
    allows = [line for line in profile.splitlines() if line.startswith("(allow file-write*")]
    assert f'(allow file-write* (subpath "{os.path.realpath(HOME)}"))' not in allows
    assert f'(allow file-write* (subpath "{cache}"))' in allows
    assert f'(allow file-write* (subpath "{os.path.realpath(scratch)}"))' in allows
    assert '(allow file-write* (subpath "/private/tmp"))' in allows


@pytest.mark.parametrize("backend", ["sandbox-exec", "bwrap"])
def test_run_sandboxed_roots_at_the_cwd_unless_told_otherwise(monkeypatch, backend):
    seen = []

    def runner(command, **kw):
        seen.append(kw["write_root"])

    monkeypatch.setattr(sandbox, "_is_sandbox_exec_available", lambda: backend == "sandbox-exec")
    monkeypatch.setattr(sandbox, "_is_bwrap_available", lambda: backend == "bwrap")
    monkeypatch.setattr(sandbox, "_run_macos_sandboxed", runner)
    monkeypatch.setattr(sandbox, "_run_bwrap_sandboxed", runner)

    sandbox.run_sandboxed("ls", cwd="/somewhere", write_mode="workspace")
    sandbox.run_sandboxed("ls", cwd="/somewhere", write_mode="workspace", write_root="/ws")
    sandbox.run_sandboxed("ls", cwd="/somewhere", write_mode="workspace", write_root=None)
    assert seen == ["/somewhere", "/ws", None]


def test_bwrap_binds_no_tree_for_none_and_the_cwd_by_default(tmp_path):
    cwd, scratch = str(tmp_path / "home"), str(tmp_path / SID)

    def binds(argv):
        head = argv[: argv.index("--chdir")]
        return [head[i + 1] for i, a in enumerate(head) if a == "--bind"]

    none = sandbox._bwrap_argv("ls", cwd, None, "workspace", write_root=None, scratch_dir=scratch)
    assert binds(none) == ["/tmp", scratch]
    assert none[none.index("--chdir") + 1] == cwd
    assert binds(sandbox._bwrap_argv("ls", cwd, None, "workspace")) == [cwd, "/tmp"]


def test_popen_sandboxed_still_binds_its_cwd_when_no_write_root_is_given(tmp_path, monkeypatch):
    """popen_sandboxed keeps its own convention (None: the cwd)."""
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen["argv"] = argv

    monkeypatch.setattr(sandbox.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sandbox, "_is_sandbox_exec_available", lambda: False)
    monkeypatch.setattr(sandbox, "_is_bwrap_available", lambda: True)
    with open(os.devnull, "w") as out:
        sandbox.popen_sandboxed("true", cwd=str(tmp_path), stdout_file=out, write_mode="workspace")
    argv = seen["argv"]
    assert argv[argv.index("--bind") + 1] == str(tmp_path)


# ── the kernel agrees ─────────────────────────────────────────────────────


@needs_sandbox_exec
def test_aws_cli_with_no_workspace_cannot_write_into_home(monkeypatch, tmp_path):
    """The field case end to end, with a stand-in ``aws`` that writes its last
    argument: no workspace, a write to a folder under $HOME is refused, and a
    write inside the workspace (once one is connected) goes through. The
    probe is under the home folder, not temp, since temp is always writable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text('#!/bin/sh\nfor a; do last="$a"; done\necho data > "$last"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    home_probe = tempfile.mkdtemp(prefix="whisper-aws-probe-", dir=HOME)
    ws = tempfile.mkdtemp(prefix="whisper-aws-ws-")
    try:
        monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
        ok, output = code.do_aws_cli({"command": f"aws s3 cp s3://b/x {home_probe}/f"})
        assert not ok, output
        assert not os.path.exists(f"{home_probe}/f")

        monkeypatch.setattr("server.workspace.get_workspace_path", lambda: ws)
        ok, output = code.do_aws_cli({"command": f"aws s3 cp s3://b/x {ws}/f"})
        assert ok, output
        assert os.path.exists(f"{ws}/f")
    finally:
        shutil.rmtree(home_probe, ignore_errors=True)
        shutil.rmtree(ws, ignore_errors=True)
