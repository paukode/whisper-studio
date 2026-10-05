"""One round limit and one time limit, from Settings, for every run.

server/infrastructure/run_limits.py is the control, like bedrock_region for
the model region: chat, voice, headless and scheduled runs and every agent
type read it, and the old per-caller numbers are gone. Agents are covered in
tests/test_agent_turn_budget.py, voice in tests/test_voice_tools.py.
"""

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from server.exec.headless import run_headless_turn
from server.infrastructure import config as config_mod
from server.infrastructure import run_limits
from tests.golden_harness import (
    FakeBedrockClient,
    msg_end,
    msg_start,
    run_chat_turn,
    text_block,
    tool_use_block,
)

ROOT = Path(__file__).resolve().parent.parent


def _limits(monkeypatch, rounds: int, seconds: float) -> None:
    monkeypatch.setattr("server.infrastructure.run_limits.round_limit", lambda: rounds)
    monkeypatch.setattr("server.infrastructure.run_limits.time_limit_seconds", lambda: seconds)


def _spy_policies(monkeypatch) -> list:
    """Record the TurnPolicy of every run_turn call."""
    import server.chat.engine.runner as runner_mod

    real = runner_mod.run_turn
    seen: list = []

    async def spy(ctx):
        seen.append(ctx.policy)
        async for chunk in real(ctx):
            yield chunk

    monkeypatch.setattr(runner_mod, "run_turn", spy)
    return seen


# ── the setting itself ──────────────────────────────────────────────────────


def test_the_limits_follow_settings_on_every_read(monkeypatch):
    for rounds, minutes in ((7, 2), (300, 45)):
        monkeypatch.setattr(
            config_mod,
            "load_config",
            lambda r=rounds, m=minutes: {"round_limit": r, "time_limit_minutes": m},
        )
        assert run_limits.round_limit() == rounds
        assert run_limits.time_limit_seconds() == 60 * minutes


def test_the_shipped_config_carries_the_defaults_and_no_per_type_limits():
    shipped = json.loads((ROOT / "config.example.json").read_text())
    for key in ("round_limit", "time_limit_minutes"):
        assert shipped[key] == config_mod.DEFAULTS[key]
    assert not set(config_mod.RETIRED_USER_KEYS) & set(shipped)


def test_a_saved_config_loses_the_retired_keys_once(tmp_path, monkeypatch):
    path = tmp_path / "config.user.json"
    path.write_text(
        json.dumps(
            {
                "agent_limits": {"default": {"max_turns": 120, "deadline_seconds": 900}},
                "cron_max_rounds": 50,
                "bedrock_region": "us-east-1",
            }
        )
    )
    monkeypatch.setattr(config_mod, "_active_user_config_path", lambda: str(path))

    assert sorted(config_mod.retire_user_config_keys()) == sorted(config_mod.RETIRED_USER_KEYS)
    assert json.loads(path.read_text()) == {"bedrock_region": "us-east-1"}
    assert config_mod.retire_user_config_keys() == []


def test_a_source_checkout_loses_the_retired_keys_at_startup(monkeypatch):
    """bootstrap_home() runs only for a packaged install, so startup is what
    reaches a setup.sh checkout, whose user layer can still be the legacy
    config.json at the repo root (the conftest gives each test its own)."""
    import server.main as main_mod

    monkeypatch.delenv("WHISPER_HOME", raising=False)
    legacy = Path(config_mod.CONFIG_PATH)
    legacy.write_text(
        json.dumps({"agent_limits": {"default": {"max_turns": 120}}, "bedrock_region": "us-east-1"})
    )

    main_mod._retire_config_keys()

    assert json.loads(legacy.read_text()) == {"bedrock_region": "us-east-1"}


# ── chat: the round limit, no time limit ────────────────────────────────────


def test_a_chat_turn_stops_at_the_settings_round_limit(monkeypatch):
    rounds = [
        [msg_start(), *tool_use_block(f"t{i}", "task_list", {}), *msg_end(stop_reason="tool_use")]
        for i in range(6)
    ]
    client = FakeBedrockClient(rounds)
    monkeypatch.setattr("server.tool_executor.route_tool", _ok_route)
    _limits(monkeypatch, 2, 600.0)
    policies = _spy_policies(monkeypatch)

    lines = run_chat_turn(monkeypatch, client, {"question": "keep listing"})

    assert len(client.requests) == 2
    assert "Reached maximum tool rounds" in "".join(lines)
    assert policies[0].deadline_seconds is None


async def _ok_route(tool_name, tool_input, **kwargs):
    return "ok", []


# ── headless: the round limit; the time limit only when nobody attends ─────


@pytest.mark.parametrize("attended", [False, True])
def test_a_headless_run_takes_the_rounds_and_time_only_when_unattended(monkeypatch, attended):
    from tests.test_headless_turn import _collect, _patch_common

    _patch_common(monkeypatch, FakeBedrockClient([[msg_start(), *text_block("hi"), *msg_end()]]))
    _limits(monkeypatch, 9, 600.0)
    policies = _spy_policies(monkeypatch)

    asyncio.run(
        _collect(
            run_headless_turn(
                "hello",
                model_key="sonnet",
                ephemeral=True,
                session_id=f"s-{uuid.uuid4().hex[:8]}",
                attended=attended,
            )
        )
    )

    (policy,) = policies
    assert policy.max_rounds == 9
    assert policy.deadline_seconds == (None if attended else 600.0)


def test_a_headless_caller_with_its_own_round_count_keeps_it(monkeypatch):
    """The wake answer is one round by design, whatever Settings say."""
    from tests.test_headless_turn import _collect, _patch_common

    _patch_common(monkeypatch, FakeBedrockClient([[msg_start(), *text_block("hi"), *msg_end()]]))
    _limits(monkeypatch, 9, 600.0)
    policies = _spy_policies(monkeypatch)

    asyncio.run(
        _collect(
            run_headless_turn(
                "hello", model_key="sonnet", ephemeral=True, session_id="s-own", max_rounds=1
            )
        )
    )

    assert policies[0].max_rounds == 1


# ── scheduled runs: both limits, shared across verify continuations ────────


def test_a_scheduled_run_shares_the_settings_limits_across_continuations(monkeypatch):
    from server.goals import Verdict
    from tests.test_goal_cron_verify import _run

    _limits(monkeypatch, 9, 600.0)
    policies = _spy_policies(monkeypatch)

    _run(
        monkeypatch,
        [Verdict("not_achieved", "add the summary", 0.6), Verdict("achieved", "done", 0.9)],
    )

    first, second = policies
    assert first.max_rounds == 9
    assert first.deadline_seconds == pytest.approx(600.0, abs=1.0)
    # The continuation gets what is left of the run, never a fresh budget.
    assert second.max_rounds < first.max_rounds
    assert 0 < second.deadline_seconds <= first.deadline_seconds
