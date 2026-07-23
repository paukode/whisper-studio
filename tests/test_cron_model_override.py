"""Per-job model override validation and the configurable round cap."""

import json

import pytest

import server.cron_scheduler as cs
from server.cron_run import CRON_MAX_ROUNDS_DEFAULT


@pytest.fixture(autouse=True)
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CRON_PATH", str(tmp_path / "cron_jobs.json"))
    yield


@pytest.fixture
def fake_models(monkeypatch):
    from server.infrastructure import config as config_mod

    real = config_mod.load_config

    def _patched():
        cfg = dict(real())
        cfg["chat_models"] = {
            "haiku": "global.anthropic.claude-haiku-4-5",
            "opus4.8": "global.anthropic.claude-opus-4-8",
            "gpt5.6-sol": "bedrock-mantle.openai.gpt-5.6-sol",
        }
        return cfg

    monkeypatch.setattr(config_mod, "load_config", _patched)
    # cron_scheduler imports load_config lazily inside functions, so patching
    # the source module covers it.
    yield


def test_validate_job_model_accepts_anthropic(fake_models):
    assert cs._validate_job_model("opus4.8") == "opus4.8"
    assert cs._validate_job_model("") == ""
    assert cs._validate_job_model(None) == ""


def test_validate_job_model_rejects_non_anthropic(fake_models):
    with pytest.raises(ValueError):
        cs._validate_job_model("gpt5.6-sol")  # OpenAI: cron loop can't drive it
    with pytest.raises(ValueError):
        cs._validate_job_model("no-such-model")


def test_cron_create_tool_carries_model(fake_models):
    out = json.loads(
        cs.execute_cron_tool(
            "cron_create",
            {
                "name": "modeled",
                "prompt": "do it",
                "schedule": {"type": "interval", "every_minutes": 60},
                "model": "opus4.8",
            },
            session_id="s1",
        )
    )
    assert out.get("created") is True
    assert out["job"]["model"] == "opus4.8"


def test_cron_create_tool_rejects_bad_model(fake_models):
    out = json.loads(
        cs.execute_cron_tool(
            "cron_create",
            {
                "name": "bad-model",
                "prompt": "do it",
                "schedule": {"type": "interval", "every_minutes": 60},
                "model": "gpt5.6-sol",
            },
            session_id="s1",
        )
    )
    assert "error" in out
    assert "Anthropic" in out["error"]


def test_round_cap_default_named_constant():
    assert CRON_MAX_ROUNDS_DEFAULT == 30
