"""The permission-mode line and the act-before-asking rules in the system prompt.

Contracts: the Bypass line appears only in a Bypass session, it sits in the
cached static block, the bytes stay the same from one round to the next, and a
flip between two non-Bypass modes leaves the static block unchanged.
"""

import pytest

from server.ask_user import ASK_USER_TOOL
from server.prompts import PromptLayer, build_system_prompt, build_system_prompt_split, get_registry
from server.prompts.base import ACT_BEFORE_ASKING, BASE, KEEP_WORKING
from server.prompts.modes import BYPASS_MODE, GUARDED_MODE
from server.prompts.workspace import workspace_prompt
from server.security.permissions import MODE_BYPASS, VALID_MODES

_NON_BYPASS = sorted(VALID_MODES - {MODE_BYPASS})


def _split(mode: str, ws: str | None = "/tmp/ws"):
    return build_system_prompt_split(ws_path=ws, session_id="s", permission_mode=mode)


def test_section_is_on_a_cached_layer():
    section = get_registry().get("permission_mode")
    assert section is not None
    assert int(section.layer) <= int(PromptLayer.WORKSPACE)


@pytest.mark.parametrize("ws", [None, "/tmp/ws"])
def test_bypass_line_only_in_bypass_and_in_the_static_block(ws):
    static, dynamic = _split(MODE_BYPASS, ws)
    assert BYPASS_MODE.strip() in static
    assert BYPASS_MODE.strip() not in dynamic
    assert GUARDED_MODE.strip() not in static + dynamic
    for mode in _NON_BYPASS:
        static, dynamic = _split(mode, ws)
        assert BYPASS_MODE.strip() not in static + dynamic
        assert GUARDED_MODE.strip() in static


def test_unknown_mode_never_claims_bypass():
    prompt = build_system_prompt(ws_path=None, session_id="s")
    assert BYPASS_MODE.strip() not in prompt
    assert GUARDED_MODE.strip() in prompt


@pytest.mark.parametrize("mode", [MODE_BYPASS, *_NON_BYPASS])
def test_static_bytes_are_stable_across_rounds(mode):
    first = _split(mode)
    second = _split(mode)
    assert first == second


def test_non_bypass_modes_share_one_static_block():
    blocks = {_split(mode)[0] for mode in _NON_BYPASS}
    assert len(blocks) == 1
    assert _split(MODE_BYPASS)[0] not in blocks


@pytest.mark.parametrize("mode", [MODE_BYPASS, "default"])
def test_split_joins_to_the_string_prompt(mode):
    static, dynamic = _split(mode)
    assert static + dynamic == build_system_prompt(
        ws_path="/tmp/ws", session_id="s", permission_mode=mode
    )


def test_bypass_line_names_what_still_asks():
    text = BYPASS_MODE.lower()
    assert "outside the workspace" in text
    assert "github" in text
    assert "approve" in text
    assert "rm -rf /" in text


def test_guarded_rule_forbids_variables_in_delete_paths():
    assert "literal absolute path" in GUARDED_MODE
    assert "$" in GUARDED_MODE


def test_identity_carries_keep_working_and_denial_rules():
    assert KEEP_WORKING in BASE
    assert ACT_BEFORE_ASKING in BASE
    assert "reply go" in KEEP_WORKING
    assert "commit" in KEEP_WORKING
    assert "do not retry the same call" in ACT_BEFORE_ASKING


def test_workspace_denial_line_does_not_order_a_question():
    text = workspace_prompt("/tmp/ws", "s")
    denied = next(line for line in text.splitlines() if "[User denied]" in line)
    assert "ask what they want" not in denied
    assert "next safe path" in denied


def test_ask_user_description_never_asks_for_tool_permission():
    desc = ASK_USER_TOOL["description"]
    assert "approval," not in desc
    assert "approval gate" in desc
    assert "permission for a tool call" in desc
    assert "numbered list" in desc
