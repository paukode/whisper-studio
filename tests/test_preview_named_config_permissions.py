"""An approved .whisper/launch.json preview starts in every permission mode.

The user's rule: whatever permission mode is selected, a preview should be
allowed to start. It covers a preview_start that names a launch config, so the
command comes wholly from launch.json, once a person has approved that command
(tests/test_preview_launch_trust.py covers how trust is earned and lost); an
ad-hoc runtimeExecutable/runtimeArgs call keeps following the mode. Explicit
denies (a custom rule, a session "No for all" on the preview category) still
refuse a named config outside bypass, and the hard floors are checked against
the command the config resolves to, including inside a shell's -c script.

The tests write a real launch.json into a temporary workspace, drive the real
preview router to build the approval sentinel, and let the registered
ApprovalSpec executor run with only the dev-server spawn stubbed.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from server import tool_executor as te
from server.approval.bootstrap import register_defaults
from server.hooks.schema import HookOutcome
from server.preview import launch_trust
from server.preview.router import execute_preview_tool
from server.security.permissions import (
    MODE_AUTO,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_DONT_ASK,
    MODE_PLAN,
    VALID_MODES,
    approval_floor,
    resolve_static_decision,
)

register_defaults()

ALL_MODES = sorted(VALID_MODES)
# bypassPermissions already overrides rules and session denials for every
# call, named or not; the deny contracts are about every other mode.
MODES_THAT_HONOR_DENIES = sorted(VALID_MODES - {MODE_BYPASS})

CHAT = "chat-preview-0001"

LAUNCH_CONFIGS = [
    {"name": "dev", "runtimeExecutable": "npm", "runtimeArgs": ["run", "dev"], "port": 5173},
    {"name": "wipe", "runtimeExecutable": "rm", "runtimeArgs": ["-rf", "build"]},
    {
        "name": "chained",
        "runtimeExecutable": "sh",
        "runtimeArgs": ["-c", "npm run build && rm -rf dist"],
    },
    # rm first inside the script: flattened with plain spaces this used to
    # read as the command `sh`.
    {
        "name": "shwipe",
        "runtimeExecutable": "sh",
        "runtimeArgs": ["-c", "rm -rf build && npm run dev"],
    },
    {"name": "bashwipe", "runtimeExecutable": "bash", "runtimeArgs": ["-lc", "rm -rf dist"]},
    {"name": "nohupwipe", "runtimeExecutable": "nohup", "runtimeArgs": ["rm", "-rf", "build"]},
    {
        "name": "shdev",
        "runtimeExecutable": "sh",
        "runtimeArgs": ["-c", "npm run build && npm run dev | tee dev.log"],
    },
]
RM_CLASS = ["wipe", "chained", "shwipe", "bashwipe", "nohupwipe"]


def _argv(name: str) -> list[str]:
    entry = next(c for c in LAUNCH_CONFIGS if c["name"] == name)
    return [entry["runtimeExecutable"], *entry["runtimeArgs"]]


@pytest.fixture(autouse=True)
def _trust_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "project"
    (ws / ".whisper").mkdir(parents=True)
    (ws / "web").mkdir()
    (ws / ".whisper" / "launch.json").write_text(json.dumps({"configurations": LAUNCH_CONFIGS}))
    # launch_config and start_policy both import it lazily from here.
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: str(ws))
    return ws


@pytest.fixture
def trusted(workspace):
    """Every config's command approved once by a person, as a Yes on its card records."""
    for config in LAUNCH_CONFIGS:
        launch_trust.trust(str(workspace), _argv(config["name"]))
    return workspace


@pytest.fixture
def perms(monkeypatch):
    """Set the persisted permissions the resolver reads (rules, category_modes)."""
    state = {"mode": MODE_DEFAULT, "rules": [], "category_modes": {}}
    monkeypatch.setattr("server.security.permissions.load_permissions", lambda: dict(state))
    return state


@pytest.fixture
def spawn(monkeypatch):
    """The dev-server start the approval executor calls, stubbed."""
    started = AsyncMock(return_value=(True, "Started preview session."))
    monkeypatch.setattr("server.preview.manager.start_preview_session", started)
    return started


@pytest.fixture
def classifier(monkeypatch):
    verdict = AsyncMock(return_value={"decision": "confirm"})
    monkeypatch.setattr(te, "classify_tool_call", verdict)
    monkeypatch.setattr(te, "explain_permission", AsyncMock(return_value=None))
    return verdict


def _sentinel(**tool_input) -> str:
    out = asyncio.run(execute_preview_tool("preview_start", {**tool_input, "__session_id__": CHAT}))
    assert out.startswith("[WS_APPROVAL]"), out
    return out


def _parsed(**tool_input) -> dict:
    return json.loads(_sentinel(**tool_input)[len("[WS_APPROVAL]") :])


def _decide(mode: str, session_approvals: dict | None = None, **tool_input):
    return resolve_static_decision(
        "preview_start", _parsed(**tool_input), "preview", session_approvals or {}, mode
    )


class _State:
    def __init__(self, output: str):
        self.tool_id = "tu_1"
        self.tool_name = "preview_start"
        self.output = output
        self.side_effects: list = []
        self.status = "completed"


def _process(mode: str, session_approvals: dict | None = None, model_id="claude", **tool_input):
    """Run one preview_start through process_tool_results. Returns
    (tool_result content, approval_request frame or None)."""
    results, sse_events, _pending, _q = asyncio.run(
        te.process_tool_results(
            [_State(_sentinel(**tool_input))],
            budget_fn=lambda _n, out: out,
            session_approvals=session_approvals or {},
            config={},
            model_id=model_id,
            recent_messages=[],
            mode=mode,
            session_id="",
        )
    )
    request = None
    for raw in sse_events:
        event = json.loads(raw)
        if "approval_request" in event:
            request = event["approval_request"]
    content = results[0]["content"] if results else None
    return content, request


# ── An approved named config starts in every mode ────────────────────────────


@pytest.mark.parametrize("mode", ALL_MODES)
def test_a_named_config_starts_without_a_prompt_or_the_classifier(
    mode, trusted, perms, spawn, classifier
):
    assert _decide(mode, session_name="dev") == "allow"

    content, request = _process(mode, session_name="dev")

    assert request is None
    assert spawn.await_count == 1
    assert spawn.await_args.args[0]["session_name"] == "dev"
    assert content == "Started preview session."
    assert classifier.await_count == 0


@pytest.mark.parametrize("category_mode", ALL_MODES)
def test_a_preview_category_mode_does_not_block_a_named_config(
    category_mode, trusted, perms, spawn, classifier
):
    perms["category_modes"] = {"preview": category_mode}
    for global_mode in (MODE_DEFAULT, MODE_BYPASS):
        assert _decide(global_mode, session_name="dev") == "allow"


def test_a_named_config_starts_on_an_on_device_turn_in_auto_mode(trusted, perms, spawn):
    # Local turns pass an empty tool-exec model id, where auto mode cannot
    # classify and would fall back to asking.
    content, request = _process(MODE_AUTO, model_id="", session_name="dev")
    assert request is None
    assert spawn.await_count == 1


def test_plan_mode_still_dispatches_preview_start(trusted, monkeypatch):
    async def route(name, call_input, **kw):
        return (f"routed {name}", [])

    state = _batch(monkeypatch, route, {"session_name": "dev"}, plan_mode=True)
    assert state.status == "completed"
    assert state.output == "routed preview_start"


# ── An ad-hoc command keeps the mode's decision ──────────────────────────────


@pytest.mark.parametrize("mode", ALL_MODES)
def test_an_ad_hoc_command_gets_the_same_decision_as_any_preview_action(mode, workspace, perms):
    adhoc = _decide(mode, session_name="dev", runtimeExecutable="npm", runtimeArgs=["run", "dev"])
    navigate = resolve_static_decision(
        "preview_navigate",
        {"action": "preview_navigate", "session_name": "dev", "url": "http://localhost:5173"},
        "preview",
        {},
        mode,
    )
    assert adhoc == navigate


def test_an_ad_hoc_command_asks_in_default_mode(workspace, perms, spawn, classifier):
    _content, request = _process(
        MODE_DEFAULT, session_name="dev", runtimeExecutable="npm", runtimeArgs=["run", "dev"]
    )
    assert request is not None
    assert request["action"] == "preview_start"
    assert spawn.await_count == 0


def test_an_ad_hoc_command_still_reaches_the_auto_classifier(workspace, perms, spawn, classifier):
    _process(MODE_AUTO, session_name="dev", runtimeExecutable="npm", runtimeArgs=["run", "dev"])
    assert classifier.await_count == 1


# ── Explicit denies still win ─────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES_THAT_HONOR_DENIES)
def test_a_deny_rule_refuses_a_named_config(mode, trusted, perms):
    perms["rules"] = [{"tool": "preview_start", "pattern": "*", "action": "deny"}]
    assert _decide(mode, session_name="dev") == "deny"


@pytest.mark.parametrize("mode", MODES_THAT_HONOR_DENIES)
def test_a_session_no_for_all_refuses_a_named_config(mode, trusted, perms, spawn, classifier):
    assert _decide(mode, {"preview": "deny"}, session_name="dev") == "deny"

    content, request = _process(mode, {"preview": "deny"}, session_name="dev")

    assert request is None
    assert spawn.await_count == 0
    assert content.startswith("[Denied by session rule]")


def test_an_ask_rule_the_user_wrote_keeps_asking(trusted, perms):
    perms["rules"] = [{"tool": "preview_start", "pattern": "*", "action": "ask"}]
    assert _decide(MODE_DONT_ASK, session_name="dev") == "ask"


def test_bypass_treats_a_named_config_like_any_other_call(trusted, perms):
    perms["rules"] = [{"tool": "preview_start", "pattern": "*", "action": "deny"}]
    named = _decide(MODE_BYPASS, session_name="dev")
    adhoc = _decide(MODE_BYPASS, session_name="dev", runtimeExecutable="npm")
    assert named == adhoc


# ── Hard floors are checked against the resolved command ─────────────────────


@pytest.mark.parametrize("name", RM_CLASS)
@pytest.mark.parametrize("mode", ALL_MODES)
def test_an_rm_class_launch_config_still_asks(name, mode, trusted, perms):
    # Trusted or not: a floor is a floor.
    parsed = _parsed(session_name=name)
    assert approval_floor("preview_start", parsed, "preview") == "rm"
    assert _decide(mode, session_name=name) == "ask"


def test_a_shell_script_that_does_not_delete_hits_no_floor(trusted, perms):
    assert approval_floor("preview_start", _parsed(session_name="shdev"), "preview") is None
    assert _decide(MODE_DEFAULT, session_name="shdev") == "allow"


def test_the_floor_reads_the_config_even_when_the_input_carries_a_command(workspace, perms):
    parsed = _parsed(session_name="wipe", command="npm run dev")
    assert approval_floor("preview_start", parsed, "preview") == "rm"


def test_the_card_for_an_rm_class_config_shows_what_would_run(workspace, perms, spawn, classifier):
    _content, request = _process(MODE_BYPASS, {"preview": "allow"}, session_name="wipe")
    assert request is not None
    assert request["always_asks"] is True
    assert request["payload"]["command"] == "rm -rf build"
    assert spawn.await_count == 0


def test_a_safe_launch_config_hits_no_floor(workspace, perms):
    assert approval_floor("preview_start", _parsed(session_name="dev"), "preview") is None


# ── What is not exempted ──────────────────────────────────────────────────────


def test_a_name_that_does_not_resolve_follows_the_mode(workspace, perms):
    for mode in ALL_MODES:
        missing = _decide(mode, session_name="nope")
        adhoc = _decide(mode, session_name="nope", runtimeExecutable="npm")
        assert missing == adhoc
    assert _decide(MODE_DEFAULT, session_name="nope") == "ask"


def test_no_connected_workspace_means_no_named_config(trusted, perms, monkeypatch):
    monkeypatch.setattr("server.workspace.state.get_workspace_path", lambda: None)
    assert _decide(MODE_DEFAULT, session_name="dev") == "ask"


def test_a_cwd_inside_the_workspace_keeps_the_exemption(workspace, trusted, perms):
    assert _decide(MODE_DEFAULT, session_name="dev", cwd=str(workspace / "web")) == "allow"
    assert _decide(MODE_DEFAULT, session_name="dev", cwd=str(workspace)) == "allow"


def test_a_cwd_outside_the_workspace_follows_the_mode(trusted, perms, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert _decide(MODE_DEFAULT, session_name="dev", cwd=str(outside)) == "ask"
    assert _decide(MODE_DONT_ASK, session_name="dev", cwd=str(outside)) == "deny"


def test_a_cwd_that_escapes_through_a_symlink_follows_the_mode(workspace, trusted, perms, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(outside, workspace / "link")
    assert _decide(MODE_DEFAULT, session_name="dev", cwd=str(workspace / "link")) == "ask"


def test_a_sibling_folder_sharing_the_prefix_is_outside(trusted, perms, tmp_path):
    sibling = tmp_path / "project-other"
    sibling.mkdir()
    assert _decide(MODE_DEFAULT, session_name="dev", cwd=str(sibling)) == "ask"


# ── The denial counter holds back only calls that show a card ────────────────


def _batch(monkeypatch, route, tool_input: dict, *, plan_mode=False, denials=None):
    monkeypatch.setattr(te, "route_tool", route)
    monkeypatch.setattr(te, "run_hooks", AsyncMock(return_value=HookOutcome()))
    executor = ThreadPoolExecutor(max_workers=1)

    async def _go():
        return await te.execute_tool_batch(
            [{"id": "t1", "name": "preview_start", "input": tool_input}],
            is_concurrent_safe=lambda n: False,
            loop=asyncio.get_running_loop(),
            executor=executor,
            transcript="",
            attachments=None,
            session_id=CHAT,
            session_denials=denials or {},
            model_id="claude",
            plan_mode=plan_mode,
            mode=MODE_PLAN if plan_mode else MODE_DEFAULT,
        )

    try:
        return asyncio.run(_go())[0]
    finally:
        executor.shutdown(wait=False)


async def _route_for_real(name, call_input, **kw):
    return (await execute_preview_tool(name, call_input), [])


def _after_denials(monkeypatch, tool_input: dict):
    denials = {"preview_start": te.MAX_AUTO_DENIALS}
    return _batch(monkeypatch, _route_for_real, tool_input, denials=denials)


def test_earlier_denials_do_not_block_an_approved_named_config(trusted, perms, monkeypatch):
    named = _after_denials(monkeypatch, {"session_name": "dev"})
    adhoc = _after_denials(monkeypatch, {"session_name": "dev", "runtimeExecutable": "npm"})

    assert named.status == "completed"
    assert named.output.startswith("[WS_APPROVAL]")
    assert adhoc.status == "skipped"
    assert adhoc.output.startswith("[Denied]")


@pytest.mark.parametrize("name", ["wipe", "shwipe"])
def test_earlier_denials_still_hold_back_a_named_config_that_asks(
    name, trusted, perms, monkeypatch
):
    # The rm floor shows a card every time, so refused cards keep counting.
    state = _after_denials(monkeypatch, {"session_name": name})
    assert state.status == "skipped"
    assert state.output.startswith("[Denied]")


@pytest.mark.parametrize("action", ["ask", "deny"])
def test_earlier_denials_still_hold_back_a_named_config_a_rule_asks_for(
    action, trusted, perms, monkeypatch
):
    perms["rules"] = [{"tool": "preview_start", "pattern": "*", "action": action}]
    state = _after_denials(monkeypatch, {"session_name": "dev"})
    assert state.status == "skipped"


def test_earlier_denials_still_hold_back_a_config_not_approved_yet(workspace, perms, monkeypatch):
    state = _after_denials(monkeypatch, {"session_name": "dev"})
    assert state.status == "skipped"
