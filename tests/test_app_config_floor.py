"""The app's own grant and config files follow the permission mode, not the
no-card allow the rest of the app's folder gets.

Area A: a file tool's write, save or delete of permissions.json, the MCP
servers, the hooks and their trust, the trusted skills, the folder grants, the
connected workspace, the enabled plugins, the scheduled jobs, the trusted
preview commands, or the user config layer follows the user's table:
bypassPermissions allows, dontAsk denies, auto lets the classifier decide,
every other mode asks. An unattended run is refused, and a confined command
(the kind no person reviewed) cannot write one through the OS sandbox.

The same table covers the config_set (sensitive keys) and cron tools, and the
project settings layer may no longer carry the permission mode or the auto
classifier rules.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil

import pytest

from server import sandbox
from server.approval.bootstrap import register_defaults
from server.infrastructure.paths import app_config_dirs, app_config_paths
from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_AUTO,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    MODE_PLAN,
    approval_floor,
    resolve_static_decision,
    targets_app_config,
)
from server.tool_executor import process_tool_results

register_defaults()

needs_sandbox_exec = pytest.mark.skipif(
    platform.system() != "Darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)

ORIGINAL = '{"mode": "default", "rules": []}\n'
MODES = (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_AUTO, MODE_PLAN, MODE_BYPASS, MODE_DONT_ASK)
# The user's table: how a write to an app config file behaves per mode.
TABLE = {
    MODE_DEFAULT: "ask",
    MODE_ACCEPT_EDITS: "ask",
    MODE_AUTO: None,  # the classifier decides
    MODE_PLAN: "ask",
    MODE_BYPASS: "allow",
    MODE_DONT_ASK: "deny",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home folder connected as the workspace, with the data-root config
    files in place."""
    data = tmp_path / "data"
    data.mkdir()
    from server.infrastructure.paths import APP_CONFIG_FILES

    for name in APP_CONFIG_FILES:
        (data / name).write_text(ORIGINAL)
    (data / "notes.txt").write_text("notes\n")
    monkeypatch.setenv("WHISPER_DATA_DIR", str(data))
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    monkeypatch.setattr(
        "server.security.permissions.load_permissions", lambda: {"mode": "default", "rules": []}
    )
    return tmp_path


def test_app_config_paths_cover_the_files_the_app_reads(home):
    from server import mcp, skills
    from server.hooks import config_loader
    from server.infrastructure import config as cfg
    from server.preview import launch_trust
    from server.security import folder_grants, permissions
    from server.workspace import paths as wpaths

    # Compare by basename: the reader constants are frozen at import, while
    # app_config_paths() is live, so their directories can differ under a test
    # that moves the data root. The invariant is that every grant/config file
    # the app reads is a file the floor protects.
    names = {os.path.basename(p) for p in app_config_paths()}
    for reader in (
        permissions.PERMISSIONS_PATH,
        mcp.MCP_CONFIG_PATH,
        skills.SKILLS_CONFIG_PATH,
        folder_grants.GRANTS_PATH,
        config_loader.hooks_path(),
        config_loader.trust_path(),
        wpaths.WORKSPACE_CONFIG_PATH,
        launch_trust.trust_path(),
        cfg.USER_CONFIG_PATH,
        cfg.CONFIG_PATH,
    ):
        assert os.path.basename(reader) in names, reader
    # The plugin modules and the custom agent types are protected as folders.
    assert app_config_dirs()


def test_every_spelling_of_a_config_file_is_that_file(home):
    data = home / "data"
    config = str(data / "permissions.json")
    (home / "link.json").symlink_to(data / "permissions.json")
    os.link(data / "hooks.json", home / "hard.json")
    (home / "project").mkdir()
    (home / "project" / "permissions.json").write_text("{}\n")
    for target in (
        config,
        "data/permissions.json",  # relative to the workspace
        "project/../data/permissions.json",
        str(data / "PERMISSIONS.JSON"),  # the Mac disk ignores case
        str(home / "link.json"),  # a symlink to it
        str(home / "hard.json"),  # a hard link to it
        str(data),  # a folder that holds config files
    ):
        assert targets_app_config({"path": target}), target
    assert targets_app_config({"destination_path": config})
    for target in (str(data / "notes.txt"), "project/permissions.json", ""):
        assert not targets_app_config({"path": target}), target


def test_a_write_to_a_config_file_follows_the_mode(home):
    config = {"path": str(home / "data" / "mcp_servers.json")}
    # It is not a hard floor: it follows the mode (bypass allows).
    assert approval_floor("save_file", config, "save") is None
    for category in ("write", "save", "delete", "office-script"):
        for mode in MODES:
            # A blanket "yes for all writes" must not cover a config file.
            for remembered in ({}, {category: "allow"}):
                decision = resolve_static_decision("save_file", config, category, remembered, mode)
                assert decision == TABLE[mode], (category, mode, remembered)
    # Other files in the data root keep the app storage rule: no card, any mode.
    notes = {"path": str(home / "data" / "notes.txt")}
    for mode in MODES:
        assert resolve_static_decision("ws_write_file", notes, "write", {}, mode) == "allow"
    # A command is not a file write: the OS sandbox covers it, not this branch.
    assert approval_floor("ws_run_command", config, "cli") is None


class _State:
    def __init__(self, payload):
        self.tool_id = "tu_1"
        self.tool_name = "save_file"
        self.output = f"[WS_APPROVAL]{json.dumps(payload)}"
        self.side_effects = []
        self.status = "pending"
        self.duration_ms = 0
        self.stats = None
        self.ws_root_taken = False
        self.ws_root = None


def _gate(payload, *, mode=MODE_DEFAULT, unattended=False):
    results, events, _pending, _q = asyncio.run(
        process_tool_results(
            [_State(payload)],
            budget_fn=lambda _n, out: out,
            session_approvals={"save": "allow", "write": "allow"},
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


def test_the_card_offers_no_remember_for_a_config_file(home):
    config = home / "data" / "permissions.json"
    payload = {"action": "save_to_path", "path": str(config), "staged_path": "/x", "size": 1}
    _out, request = _gate(payload, mode=MODE_DEFAULT)
    assert request is not None and request["always_asks"] is True
    assert config.read_text() == ORIGINAL  # not written on an ask


def test_an_unattended_agent_cannot_write_a_config_file(home):
    config = home / "data" / "skills_config.json"
    payload = {"action": "write", "path": "data/skills_config.json", "content": '{"trusted": []}'}
    out, request = _gate(payload, unattended=True)
    assert request is None and "needs a person's approval" in out
    assert config.read_text() == ORIGINAL


def test_a_confined_command_profile_denies_the_config_files(home):
    profile = sandbox._generate_macos_profile(str(home), write_mode="workspace")
    last_allow = profile.rindex("(allow file-write*")
    for path in app_config_paths():
        deny = f'(deny file-write* (literal "{os.path.realpath(path)}"))'
        assert deny in profile, path
        assert profile.index(deny) > last_allow, path
    # A command a person approved keeps its open writes (no config denies).
    assert "permissions.json" not in sandbox._generate_macos_profile(str(home), write_mode="open")


def test_the_bwrap_argv_binds_the_config_files_read_only(home):
    argv = sandbox._bwrap_argv("true", str(home), None, "workspace")
    config = os.path.realpath(str(home / "data" / "permissions.json"))
    at = argv.index(config)
    assert argv[at - 1 : at + 2] == ["--ro-bind", config, config]
    assert config not in sandbox._bwrap_argv("true", str(home), None, "open")


@needs_sandbox_exec
def test_the_kernel_keeps_a_confined_command_off_the_config_files(home):
    def run(command, write_mode="workspace"):
        return sandbox.run_sandboxed(command, cwd=str(home), timeout=20, write_mode=write_mode)

    for target in ("data/permissions.json", "data/PERMISSIONS.JSON"):
        assert run(f"echo '{{}}' > {target}").returncode != 0, target
    assert run("mv data/hooks.json data/hooks.bak").returncode != 0
    assert (home / "data" / "permissions.json").read_text() == ORIGINAL
    assert (home / "data" / "hooks.json").read_text() == ORIGINAL
    # Other files in the data root stay writable for a confined command.
    assert run("echo more >> data/notes.txt").returncode == 0
    # A command a person approved on its card writes where it asks.
    assert run("echo '{}' > data/folder_grants.json", write_mode="open").returncode == 0


# ── The tools that write config: config_set and cron ─────────────────────────


def test_config_set_gates_only_sensitive_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "server.security.permissions.load_permissions", lambda: {"mode": "default", "rules": []}
    )
    from server.agent_tools.config_tools import execute_config_set

    sensitive = execute_config_set({"updates": {"data_dir": "/x"}})
    assert sensitive.startswith("[WS_APPROVAL]")
    assert json.loads(sensitive[len("[WS_APPROVAL]") :])["action"] == "config_set"
    # A harmless key writes straight away (no card).
    plain = execute_config_set({"updates": {"whisper_language": "pl"}})
    assert not plain.startswith("[WS_APPROVAL]")
    for mode in MODES:
        decision = resolve_static_decision("config_set", {"updates": {}}, "config", {}, mode)
        assert decision == TABLE[mode], mode


def test_cron_create_follows_the_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "server.security.permissions.load_permissions", lambda: {"mode": "default", "rules": []}
    )
    from server.cron_scheduler import execute_cron_tool

    out = execute_cron_tool(
        "cron_create",
        {"name": "j", "prompt": "do x", "schedule": {"type": "interval", "seconds": 1800}},
        session_id="sess1",
    )
    assert out.startswith("[WS_APPROVAL]")
    assert json.loads(out[len("[WS_APPROVAL]") :])["action"] == "cron_create"
    for mode in MODES:
        decision = resolve_static_decision("cron_create", {"name": "j"}, "cron", {}, mode)
        assert decision == TABLE[mode], mode


def test_an_unattended_agent_cannot_change_config_or_cron():
    from server.agent_tools.config_tools import _do_config_set
    from server.cron_scheduler import _do_cron_create

    config = asyncio.run(_do_config_set({"updates": {"data_dir": "/x"}, "__agent__": True}))
    assert not config.ok and "unattended" in (config.error or "")
    cron = asyncio.run(_do_cron_create({"name": "j", "__agent__": True}))
    assert not cron.ok and "unattended" in (cron.error or "")


def test_project_settings_cannot_carry_the_mode_or_classifier_rules(tmp_path):
    from server.infrastructure.config import PROJECT_SETTINGS_KEYS, _load_project_config

    for key in (
        "permission_mode",
        "auto_mode_allow",
        "auto_mode_soft_deny",
        "auto_mode_environment",
    ):
        assert key not in PROJECT_SETTINGS_KEYS, key
    ws = tmp_path / "ws"
    (ws / ".whisper").mkdir(parents=True)
    (ws / ".whisper" / "settings.json").write_text(
        json.dumps(
            {
                "permission_mode": "bypassPermissions",
                "auto_mode_allow": ["everything"],
                "brief_mode": True,
            }
        )
    )
    loaded = _load_project_config(str(ws))
    assert "permission_mode" not in loaded and "auto_mode_allow" not in loaded
    assert loaded.get("brief_mode") is True  # harmless keys still pass
