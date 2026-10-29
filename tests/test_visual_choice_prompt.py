"""The rule that lets the model pick a visual on its own is part of the cloud
chat prompt only.

server/prompts/visuals.py tells the model when a reply should carry a
diagram or chart the user did not ask for, and that visuals take the app
theme on their own. It rides the cached static block (fixed text, no
builder), so it must sit there and nowhere in the per-request tail, and a
mode flip must not change that block. Headless runs build their own system
prompt and never get it.
"""

import asyncio

from server.chat.caching import resolve_system_prompt
from server.exec.headless import run_headless_turn
from server.prompts import build_system_prompt_split
from server.prompts.visuals import VISUAL_CHOICE
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block

HEADING = "## Choosing the form of a reply"


def _system(fake: FakeBedrockClient) -> str:
    system = fake.requests[0].get("system") or ""
    return system if isinstance(system, str) else "".join(b.get("text", "") for b in system)


def test_the_rule_is_in_the_static_block_of_a_cloud_chat_prompt():
    for ws_path in (None, "/tmp/ws"):
        prompt, static, dynamic, _ttl = resolve_system_prompt(
            "global.anthropic.claude-opus-4-8", caching_on=True, ws_path=ws_path, session_id="s"
        )
        assert static.count(VISUAL_CHOICE) == 1
        assert HEADING not in dynamic
        assert prompt == static + dynamic


def test_a_mode_flip_keeps_the_rule_and_plan_mode_keeps_the_static_block():
    off, _ = build_system_prompt_split(ws_path="/tmp/ws", session_id="s")
    plan, _ = build_system_prompt_split(ws_path="/tmp/ws", session_id="s", plan_mode=True)
    brief, _ = build_system_prompt_split(ws_path="/tmp/ws", session_id="s", brief_mode=True)
    assert plan == off
    assert VISUAL_CHOICE in off
    assert VISUAL_CHOICE in brief
    # Brief mode is handled by the text, not by switching the section off.
    assert "in brief mode" in VISUAL_CHOICE


def test_the_rule_names_only_the_visual_tools_the_chat_always_has():
    from server.chat.tool_partition import CORE_TOOLS

    for tool in ("create_visual", "create_chart"):
        assert tool in VISUAL_CHOICE
        assert tool in CORE_TOOLS


def test_the_rule_is_one_heading_and_plain_text():
    assert VISUAL_CHOICE.startswith("\n\n" + HEADING + "\n")
    assert "\N{EM DASH}" not in VISUAL_CHOICE
    assert "\N{EN DASH}" not in VISUAL_CHOICE


def test_a_headless_run_does_not_get_the_rule(monkeypatch):
    fake = FakeBedrockClient([[msg_start(), *text_block("Done."), *msg_end()]])
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"chat_models": {"sonnet": "test-model"}, "default_chat_model": "sonnet"},
    )

    async def run():
        return [
            e
            async for e in run_headless_turn(
                "say done", model_key="sonnet", ephemeral=True, session_id="sess-visual-hl"
            )
        ]

    asyncio.run(run())
    system = _system(fake)
    assert "headless task" in system
    assert HEADING not in system
