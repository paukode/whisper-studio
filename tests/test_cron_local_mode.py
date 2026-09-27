"""Scheduled runs and Local mode: a job that fires in Local mode is refused
before any model is resolved (no Bedrock stream, a failed run whose text says
why), and cron_create refuses up front. Hybrid and Cloud keep running jobs."""

from __future__ import annotations

import asyncio
import json

import pytest

import server.cron_scheduler as C
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block


def _job() -> dict:
    return {
        "id": "job-local",
        "name": "local-job",
        "prompt": "summarise my inbox",
        "session_id": "sess-local",
        "schedule": {"type": "interval", "seconds": 1800},
        "enabled": True,
    }


def _fire(monkeypatch, mode: str) -> tuple[dict, FakeBedrockClient]:
    """Fire one job through the real run loop with a scripted Bedrock stream."""
    import server.chat.tool_pool as TP
    import server.cron_history as H
    import server.infrastructure.config as CFG
    import server.prompts.rules as R
    import server.workspace as WS

    job = _job()
    stream = FakeBedrockClient([[msg_start(), *text_block("done"), *msg_end()]])
    monkeypatch.setattr(C, "load_cron_jobs", lambda: [job])
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: stream)
    monkeypatch.setattr(TP, "assemble_partitioned_pool", lambda **k: ([], [], 0))
    monkeypatch.setattr(WS, "get_workspace_path", lambda: "")
    monkeypatch.setattr(R, "append_rules", lambda s: s)
    monkeypatch.setattr(H, "start_run", lambda *a, **k: None)
    monkeypatch.setattr(
        CFG,
        "load_config",
        lambda *a, **k: {
            "model_mode": mode,
            "chat_models": {"haiku": "fake-model-id"},
            "feature_flags": {"cron_verify": False},
        },
    )
    recorded: dict = {}

    def fake_push(job, text, status="ok", *, run_id, duration_ms=None):
        recorded.update(text=text, status=status)

    monkeypatch.setattr(C, "_push_result", fake_push)
    asyncio.run(C._execute_cron_prompt(job["id"]))
    return recorded, stream


def test_local_mode_fire_is_refused_before_any_model_call(monkeypatch):
    recorded, stream = _fire(monkeypatch, "local")
    assert recorded["status"] == "failed"
    assert "Local mode" in recorded["text"] and "Scheduled runs" in recorded["text"]
    assert stream.requests == []


@pytest.mark.parametrize("mode", ["hybrid", "cloud"])
def test_cloud_modes_still_run_the_job(monkeypatch, mode):
    recorded, stream = _fire(monkeypatch, mode)
    assert recorded["status"] == "ok" and "done" in recorded["text"]
    assert stream.requests


class _Reached(Exception):
    pass


@pytest.mark.parametrize("mode", ["local", "hybrid", "cloud"])
def test_cron_create_refuses_exactly_in_local_mode(monkeypatch, mode):
    import server.infrastructure.config as CFG

    real = CFG.load_config()
    monkeypatch.setattr(CFG, "load_config", lambda *a, **k: {**real, "model_mode": mode})
    monkeypatch.setattr(C, "load_cron_jobs", lambda: [])

    def create(*a, **k):
        raise _Reached

    monkeypatch.setattr(C, "_create_job", create)
    args = {
        "name": "daily",
        "prompt": "brief me",
        "schedule": {"type": "cron", "hour": 7, "minute": 0},
    }
    if mode == "local":
        out = json.loads(C.execute_cron_tool("cron_create", args, session_id="s"))
        assert "Local mode" in out["error"]
    else:
        with pytest.raises(_Reached):
            C.execute_cron_tool("cron_create", args, session_id="s")
