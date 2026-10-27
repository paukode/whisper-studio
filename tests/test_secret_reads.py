"""Reading the credential stores follows the permission mode.

The user's table (server/security/secret_reads.py): bypassPermissions reads
the Keychain, ~/.ssh, ~/.aws and the rest with no card; dontAsk refuses;
every other mode asks a person each time, never the classifier or a blanket
"Yes, all"; an unattended run outside bypass is refused. The OS sandbox
never lets a command write them, whatever the mode.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil

import pytest

from server import sandbox
from server.approval import registry
from server.approval.bootstrap import register_defaults
from server.approval.markers import APPROVAL, approval_request, is_app_request
from server.approval.spec import execute_approved_by_human
from server.security import secret_reads
from server.security.command_validator import validate_command
from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_AUTO,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    MODE_PLAN,
)
from server.tool_approvals import _settle_secret_access
from server.tool_executor import process_tool_results
from server.workspace import executors

register_defaults()

needs_sandbox_exec = pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)

TABLE = {
    MODE_BYPASS: "allow",
    MODE_DONT_ASK: "deny",
    MODE_DEFAULT: "ask",
    MODE_ACCEPT_EDITS: "ask",
    MODE_PLAN: "ask",
    MODE_AUTO: "ask",
}


def test_the_mode_table():
    for mode, expected in TABLE.items():
        assert secret_reads.decision(mode) == expected, mode


# ── the OS sandbox ───────────────────────────────────────────────────────────


def test_a_secret_read_profile_keeps_only_the_write_deny():
    keychains = os.path.realpath(os.path.expanduser("~/Library/Keychains"))
    closed = sandbox._generate_macos_profile("/tmp")
    opened = sandbox._generate_macos_profile("/tmp", secret_reads=True)
    assert f'(deny file-read* file-write* (subpath "{keychains}"))' in closed
    assert f'(deny file-write* (subpath "{keychains}"))' in opened
    assert "file-read*" not in opened.replace("(allow file-read*", "")


@needs_sandbox_exec
def test_the_kernel_reads_but_never_writes_with_secret_reads():
    keychains = os.path.expanduser("~/Library/Keychains")
    if not os.path.isdir(keychains):
        pytest.skip("no ~/Library/Keychains on this machine")
    probe = os.path.join(keychains, "whisper-secret-reads-probe")

    def run(command, **kw):
        return sandbox.run_sandboxed(command, cwd="/tmp", timeout=20, **kw)

    try:
        assert run(f"ls {keychains}").returncode != 0
        assert run(f"ls {keychains}", secret_reads=True).returncode == 0
        assert run(f"touch {probe}", secret_reads=True).returncode != 0
        assert not os.path.exists(probe)
    finally:
        if os.path.exists(probe):
            os.remove(probe)


def test_the_validator_lets_a_secret_read_name_its_path():
    assert validate_command("cat ~/.ssh/config")
    assert validate_command("cat ~/.ssh/config", secret_reads=True) is None


def test_a_hidden_keychain_says_how_to_ask():
    out = "security: SecKeychainCopyDefault: A default keychain could not be found."
    assert "secret_access=true" in secret_reads.denial_hint("security find-identity", out)
    assert secret_reads.denial_hint("ls", "No such file or directory") is None


# ── the approval gate settles a command's secret_access ───────────────────────


def test_bypass_reads_with_no_request_and_other_modes_strip_a_forged_one():
    spec = registry.get("run_python")
    payload = {"code": "print(1)", secret_reads.SECRET_READS_KEY: True}
    assert _settle_secret_access(spec, payload, MODE_BYPASS) is None
    assert payload == {"code": "print(1)", "secret_access": True}
    payload = {"code": "print(1)", "secret_access": False, secret_reads.SECRET_READS_KEY: True}
    assert _settle_secret_access(spec, payload, MODE_DEFAULT) is None
    assert payload == {"code": "print(1)"}
    for mode, expected in TABLE.items():
        payload = {"code": "print(1)", "secret_access": True}
        assert _settle_secret_access(spec, payload, mode) == expected, mode
        assert payload["secret_access"] is True


class _State:
    def __init__(self, tool_name, payload):
        self.tool_id = "tu_1"
        self.tool_name = tool_name
        self.output = approval_request(payload)
        self.side_effects = []
        self.status = "pending"
        self.duration_ms = 0
        self.stats = None
        self.ws_root_taken = False
        self.ws_root = None


def _gate(tool_name, payload, *, mode, unattended=False):
    results, events, _pending, _q = asyncio.run(
        process_tool_results(
            [_State(tool_name, payload)],
            budget_fn=lambda _n, out: out,
            # A blanket "Yes, all" never covers a secret read.
            session_approvals={"cli": "allow", secret_reads.SECRET_READ_CATEGORY: "allow"},
            config={},
            model_id="",
            recent_messages=[],
            mode=mode,
            unattended=unattended,
        )
    )
    request = None
    for raw in events:
        event = json.loads(raw)
        if "approval_request" in event:
            request = event["approval_request"]
    return json.dumps(results), request


def test_a_command_asking_for_secrets_asks_every_time(monkeypatch):
    ran = []
    monkeypatch.setattr(
        "server.executors.code.do_run_python", lambda p: ran.append(p) or (True, "")
    )
    payload = {"action": "run_python", "code": "print(1)", "secret_access": True}
    for mode in (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_PLAN, MODE_AUTO):
        _out, request = _gate("run_python", payload, mode=mode)
        assert request is not None and request["always_asks"] is True, mode
        assert request["payload"]["secret_access"] is True
        assert request["summary"].startswith("READS THE KEYCHAIN")
    out, request = _gate("run_python", payload, mode=MODE_DONT_ASK)
    assert request is None and "Don't ask" in out
    out, request = _gate("run_python", payload, mode=MODE_DEFAULT, unattended=True)
    assert request is None and "no person to ask" in out
    assert ran == []
    _out, request = _gate("run_python", payload, mode=MODE_BYPASS)
    assert request is None and ran and ran[0]["secret_access"] is True


# ── a file read of a secret path ─────────────────────────────────────────────


@pytest.fixture
def ssh(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    folder = tmp_path / ".ssh"
    folder.mkdir()
    (folder / "config").write_text("Host secret-host\n")
    return folder


def _read_request(ssh):
    out = executors._exec_ws_read_file({"path": str(ssh / "config")}, [], [])
    assert is_app_request(out, APPROVAL)
    return json.loads(out[len(APPROVAL) :])


def test_a_secret_file_read_asks_and_a_yes_reads_it(ssh):
    payload = _read_request(ssh)
    assert "secret-host" not in json.dumps(payload)
    _out, request = _gate("ws_read_file", payload, mode=MODE_DEFAULT)
    assert request["category"] == secret_reads.SECRET_READ_CATEGORY
    assert request["always_asks"] is True
    outcome = asyncio.run(
        execute_approved_by_human(registry.get(secret_reads.SECRET_READ_ACTION), request["payload"])
    )
    assert outcome.ok and "secret-host" in outcome.output


def test_a_secret_file_read_is_refused_without_a_person(ssh):
    payload = _read_request(ssh)
    out, request = _gate("ws_read_file", payload, mode=MODE_DONT_ASK)
    assert request is None and "secret-host" not in out
    out, request = _gate("ws_read_file", payload, mode=MODE_DEFAULT, unattended=True)
    assert request is None and "secret-host" not in out and "no person to ask" in out


def test_bypass_reads_a_secret_file_directly(ssh):
    stamped = {"path": str(ssh / "config"), secret_reads.SECRET_READS_KEY: True}
    assert "secret-host" in executors._exec_ws_read_file(stamped, [], [])


def test_the_executor_stamps_bypass_and_overwrites_a_forged_stamp(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from server import tool_executor as te

    seen: list[dict] = []

    async def route(name, tool_input, **kwargs):
        seen.append(dict(tool_input))
        return "ok", []

    monkeypatch.setattr(te, "route_tool", route)
    forged = {"path": "~/.ssh/config", secret_reads.SECRET_READS_KEY: True}
    executor = ThreadPoolExecutor(max_workers=1)

    async def batch(mode):
        return await te.execute_tool_batch(
            [{"id": "a", "name": "ws_read_file", "input": dict(forged)}],
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="s1",
            session_denials={},
            model_id="claude",
            plan_mode=False,
            mode=mode,
        )

    try:
        asyncio.run(batch(MODE_DEFAULT))
        asyncio.run(batch(MODE_BYPASS))
    finally:
        executor.shutdown(wait=True)
    assert [call[secret_reads.SECRET_READS_KEY] for call in seen] == [False, True]
