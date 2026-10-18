"""Writes under ~/.whisper never ask, in any permission mode but plan.

The user's rule: the app's own folders (rules, the profile, scratch, memory,
data) are where these writes belong, so no mode, category override or
session answer may raise a card for them. A delete there is silent only in
bypassPermissions; every other mode keeps its normal delete rule. Workspace
paths are untouched by this. Plan mode writes nothing anywhere, so the
memory and rules writers are blocked there like every other write.
"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

from server import tool_executor as te
from server.hooks.schema import HookOutcome
from server.infrastructure import paths
from server.security.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    MODE_PLAN,
    resolve_static_decision,
    targets_app_storage,
)


def _decide(tool, payload, category, mode):
    return resolve_static_decision(tool, payload, category, {}, mode)


def test_user_root_and_data_root_are_app_storage(tmp_path):
    root = paths.user_root()
    assert targets_app_storage({"path": os.path.join(root, "rules", "global", "rules.md")})
    assert targets_app_storage({"path": os.path.join(paths.data_root(), "scratch", "s", "p.py")})
    assert targets_app_storage({"destination_path": os.path.join(root, "WHISPER.md")})
    assert not targets_app_storage({"path": str(tmp_path / "elsewhere.txt")})
    assert not targets_app_storage({"path": ""})
    assert not targets_app_storage({"command": "ls"})


def test_relative_paths_resolve_against_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    assert not targets_app_storage({"path": "src/app.py"})
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    assert not targets_app_storage({"path": "src/app.py"})


def test_writes_under_app_storage_allow_in_every_mode(monkeypatch):
    monkeypatch.setattr(
        "server.security.permissions.load_permissions",
        lambda: {"category_modes": {"write": "ask"}},
    )
    scratch = os.path.join(paths.data_root(), "scratch", "sess", "probe.py")
    for mode in (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_DONT_ASK, MODE_BYPASS):
        assert (
            _decide("ws_create_file", {"action": "create", "path": scratch}, "write", mode)
            == "allow"
        )
    rule = os.path.join(paths.user_root(), "rules", "global", "rules.md")
    assert (
        _decide("ws_write_file", {"action": "write", "path": rule}, "write", MODE_DEFAULT)
        == "allow"
    )
    assert (
        _decide("save_file", {"action": "save_to_path", "path": rule}, "save", MODE_DEFAULT)
        == "allow"
    )


def test_deletes_under_app_storage_are_silent_only_in_bypass():
    scratch = os.path.join(paths.data_root(), "scratch", "sess", "probe.py")
    payload = {"action": "delete", "path": scratch}
    assert _decide("ws_delete_file", payload, "delete", MODE_BYPASS) == "allow"
    assert _decide("ws_delete_file", payload, "delete", MODE_DEFAULT) == "ask"
    assert _decide("ws_delete_file", payload, "delete", MODE_ACCEPT_EDITS) == "ask"
    assert _decide("ws_delete_file", payload, "delete", MODE_DONT_ASK) == "deny"


def test_workspace_writes_keep_the_mode_rules(tmp_path, monkeypatch):
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(tmp_path))
    payload = {"action": "create", "path": "src/new.py"}
    assert _decide("ws_create_file", payload, "write", MODE_DEFAULT) == "ask"
    assert _decide("ws_create_file", payload, "write", MODE_ACCEPT_EDITS) == "allow"


def test_dev_checkout_counts_only_the_app_owned_children(monkeypatch):
    """With user_root() equal to the repo root (no WHISPER_HOME, no override),
    the repo is also the likely workspace: only rules/, data/ and the other
    app-owned folders are storage, never the source tree."""
    monkeypatch.delenv("WHISPER_USER_DIR", raising=False)
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    monkeypatch.delenv("WHISPER_DATA_DIR", raising=False)
    root = paths.repo_root()
    assert paths.user_root() == root
    assert targets_app_storage({"path": os.path.join(root, "rules", "x", "rules.md")})
    assert targets_app_storage({"path": os.path.join(root, "WHISPER.md")})
    assert not targets_app_storage({"path": os.path.join(root, "server", "main.py")})


def _run(monkeypatch, tool: str, *, plan_mode: bool):
    async def route(name, call_input, **kw):
        return (f"routed {name}", [])

    monkeypatch.setattr(te, "route_tool", route)
    monkeypatch.setattr(te, "run_hooks", AsyncMock(return_value=HookOutcome()))
    executor = ThreadPoolExecutor(max_workers=1)

    async def _go():
        return await te.execute_tool_batch(
            [{"id": "t1", "name": tool, "input": {"content": "x"}}],
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id="chat-app-storage",
            session_denials={},
            model_id="claude",
            plan_mode=plan_mode,
            mode=MODE_PLAN if plan_mode else MODE_DEFAULT,
        )

    try:
        return asyncio.run(_go())[0]
    finally:
        executor.shutdown(wait=False)


def test_plan_mode_blocks_the_writers_of_the_apps_own_folder(monkeypatch):
    for tool in ("memory_write", "memory_delete", "rules_write"):
        state = _run(monkeypatch, tool, plan_mode=True)
        assert state.status == "skipped", tool
        assert state.output.startswith("[Plan Mode]"), tool
        state = _run(monkeypatch, tool, plan_mode=False)
        assert state.output == f"routed {tool}"
