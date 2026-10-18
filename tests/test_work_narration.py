"""The work narration and recap rule is part of the cloud chat prompt only.

server/prompts/narration.py asks the model for a line before each group of
tool calls, what a read found, and a recap with a table of the files the turn
wrote. It rides the cached static block (fixed text, no builder), so it must
sit there and nowhere in the per-request tail, and it must not change that
block when plan mode flips. Headless runs, scheduled tasks and the on-device
model build their own system prompts and never get it.
"""

import asyncio

from server.chat.caching import resolve_system_prompt
from server.exec.headless import run_headless_turn
from server.local.runtime import build_local_system_prompt
from server.prompts import build_system_prompt_split
from server.prompts.narration import WORK_NARRATION
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block
from tests.test_cron_tools import _run_cron_job

HEADING = "## Showing your work"


def _system(fake: FakeBedrockClient) -> str:
    system = fake.requests[0].get("system") or ""
    return system if isinstance(system, str) else "".join(b.get("text", "") for b in system)


# ── the cloud chat prompt ─────────────────────────────────────────────────


def test_the_rule_is_in_the_static_block_of_a_cloud_chat_prompt():
    for ws_path in (None, "/tmp/ws"):
        prompt, static, dynamic, _ttl = resolve_system_prompt(
            "global.anthropic.claude-opus-4-8", caching_on=True, ws_path=ws_path, session_id="s"
        )
        assert static.count(WORK_NARRATION) == 1
        assert HEADING not in dynamic
        assert prompt == static + dynamic


def test_plan_mode_leaves_the_static_block_as_it_was():
    # The text names brief and plan mode instead of being switched off by
    # them, so flipping plan mode never rewrites the cached prefix.
    off, _ = build_system_prompt_split(ws_path="/tmp/ws", session_id="s")
    on, _ = build_system_prompt_split(ws_path="/tmp/ws", session_id="s", plan_mode=True)
    assert on == off
    assert WORK_NARRATION in on
    brief, _ = build_system_prompt_split(ws_path="/tmp/ws", session_id="s", brief_mode=True)
    assert WORK_NARRATION in brief
    assert "In brief mode and in plan mode, skip the narration and the recap" in WORK_NARRATION


def test_the_rule_is_one_heading_and_plain_text():
    assert WORK_NARRATION.startswith("\n\n" + HEADING + "\n")
    assert "\N{EM DASH}" not in WORK_NARRATION
    assert "\N{EN DASH}" not in WORK_NARRATION


# ── the prompts that build their own identity ────────────────────────────


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
                "say done", model_key="sonnet", ephemeral=True, session_id="sess-narration-hl"
            )
        ]

    asyncio.run(run())
    system = _system(fake)
    assert "headless task" in system
    assert HEADING not in system


def test_a_scheduled_run_does_not_get_the_rule(monkeypatch):
    fake = FakeBedrockClient([[msg_start(), *text_block("done"), *msg_end()]])
    job = {
        "id": "job-narration",
        "name": "narration-job",
        "prompt": "do the thing",
        "session_id": "sess-narration-cron",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }

    async def no_tool(*a, **k):  # pragma: no cover - the script calls no tool
        return "", []

    _run_cron_job(monkeypatch, job, fake, no_tool, {})
    system = _system(fake)
    assert "unattended scheduled task" in system
    assert HEADING not in system


def test_the_on_device_prompt_does_not_get_the_rule():
    for tools in (False, True):
        prompt = build_local_system_prompt(tools=tools, ws_path="/tmp/ws")
        assert prompt
        assert HEADING not in prompt
