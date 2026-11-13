"""The reply style rule is part of the cloud chat prompt only.

server/prompts/reply_style.py asks the model to work without narration, to
give one final reply with the outcome first, to format it so it reads at a
glance, and to close with a short table of the files the turn wrote. It rides the cached static block (fixed text, no builder), so it must
sit there and nowhere in the per-request tail, and it must not change that
block when plan mode flips. Headless runs, scheduled tasks and the on-device
model build their own system prompts and never get it.
"""

import asyncio

from server.chat.caching import resolve_system_prompt
from server.exec.headless import run_headless_turn
from server.local.runtime import build_local_system_prompt
from server.prompts import build_system_prompt_split
from server.prompts.reply_style import REPLY_STYLE
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block
from tests.test_cron_tools import _run_cron_job

HEADING = "## How you reply"


def _system(fake: FakeBedrockClient) -> str:
    system = fake.requests[0].get("system") or ""
    return system if isinstance(system, str) else "".join(b.get("text", "") for b in system)


# ── the cloud chat prompt ─────────────────────────────────────────────────


def test_the_rule_is_in_the_static_block_of_a_cloud_chat_prompt():
    for ws_path in (None, "/tmp/ws"):
        prompt, static, dynamic, _ttl = resolve_system_prompt(
            "global.anthropic.claude-opus-4-8", caching_on=True, ws_path=ws_path, session_id="s"
        )
        assert static.count(REPLY_STYLE) == 1
        assert HEADING not in dynamic
        assert prompt == static + dynamic


def test_plan_mode_leaves_the_static_block_as_it_was():
    # The text names plan mode instead of being switched off by
    # it, so flipping plan mode never rewrites the cached prefix.
    off, _ = build_system_prompt_split(ws_path="/tmp/ws", session_id="s")
    on, _ = build_system_prompt_split(ws_path="/tmp/ws", session_id="s", plan_mode=True)
    assert on == off
    assert REPLY_STYLE in on
    assert "In plan mode, follow that mode's own rules for the reply." in REPLY_STYLE


def test_the_rule_asks_for_no_narration_and_a_formatted_final_reply():
    # No reply-length control exists: this text is the default, and the user's
    # own files and message decide past it.
    assert "do not announce a call before you make it" in REPLY_STYLE
    assert "Start with the outcome in one sentence" in REPLY_STYLE
    for form in ("bullets", "## heading", "markdown table", "backticks", "bold"):
        assert form in REPLY_STYLE
    assert "| File | What changed | Lines |" in REPLY_STYLE
    assert "WHISPER.md and rules decide the length, tone and format" in REPLY_STYLE


def test_the_rule_is_one_heading_and_plain_text():
    assert REPLY_STYLE.startswith("\n\n" + HEADING + "\n")
    assert "\N{EM DASH}" not in REPLY_STYLE
    assert "\N{EN DASH}" not in REPLY_STYLE


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
