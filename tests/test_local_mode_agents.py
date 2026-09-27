"""Local mode for agents, workflows and headless runs.

Local mode promises that nothing leaves this Mac. Chat turns refuse a cloud
model in their own model resolution; the agent paths reach Bedrock without
going through a chat turn, so they are pinned here:

* A spawn_agent (or workflow agent) override naming a cloud model is refused
  with the chat turn's reason, and the agent runs on the default model.
* run_agent never runs a cloud model in Local mode, whether it came as an
  override or from the configured default, and says why.
* A headless run (no on-device path at all) is refused before any client
  exists.
* A workflow launched with no model takes the on-device default.

The user layer is patched where production reads it
(``config._load_user_config``), so the real load_config runs on top.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.infrastructure import config as cfg
from server.infrastructure.model_mode import NO_LOCAL_AGENT_MODEL_REASON

LOCAL_ID = "local:gemma-4-12b-it-qat-q4_0"


@pytest.fixture
def user_layer(monkeypatch):
    layer: dict = {"model_mode": "local"}
    monkeypatch.setattr(cfg, "_load_user_config", lambda: json.loads(json.dumps(layer)))
    cfg._invalidate_cache()
    yield layer
    cfg._invalidate_cache()


@pytest.fixture
def no_cloud(monkeypatch):
    """Building any boto3 client fails the test."""
    import boto3

    def _boom(*_a, **_k):
        raise AssertionError("a cloud client was built")

    monkeypatch.setattr(boto3.session.Session, "client", _boom)
    monkeypatch.setattr(boto3, "client", _boom)


def _cloud_key() -> str:
    return cfg.load_config()["default_chat_model"]


def _with_local_model(layer: dict) -> str:
    layer["chat_models"] = {
        "local_gemma": {"id": LOCAL_ID, "label": "Gemma", "is_local": True},
    }
    cfg._invalidate_cache()
    return "local_gemma"


# ── the override resolver ────────────────────────────────────────────────────


def test_local_mode_refuses_a_cloud_override_with_the_chat_reason(user_layer):
    from server.agents.model_resolve import resolve_model_override

    model_id, key, warning = resolve_model_override("sonnet", LOCAL_ID)
    assert model_id == LOCAL_ID
    assert key == ""
    assert "Local mode" in warning and "was not called" in warning


def test_hybrid_mode_honours_the_same_cloud_override(user_layer):
    from server.agents.model_resolve import resolve_model_override

    user_layer["model_mode"] = "hybrid"
    cfg._invalidate_cache()
    model_id, key, warning = resolve_model_override(_cloud_key(), LOCAL_ID)
    assert model_id == cfg.load_config()["chat_models"][_cloud_key()]
    assert key == _cloud_key() and warning == ""


# ── run_agent, the one place every agent run resolves its model ─────────────


def test_local_mode_agent_default_skips_every_cloud_model(user_layer, monkeypatch):
    from server.agents.runtime import _resolve_agent_model, no_agent_model_reason

    monkeypatch.setattr("server.local.runtime.supports_tools", lambda key: True)
    assert _resolve_agent_model(None, None) is None
    assert no_agent_model_reason(None) == NO_LOCAL_AGENT_MODEL_REASON

    _with_local_model(user_layer)
    assert _resolve_agent_model(None, None) == LOCAL_ID


def test_local_mode_run_agent_refuses_a_cloud_override(user_layer, no_cloud):
    from server.agents.runtime import run_agent

    cloud_id = cfg.load_config()["chat_models"][_cloud_key()]
    result = asyncio.run(run_agent("list files", session_id="", model_id_override=cloud_id))
    assert result.status == "failed"
    assert "Local mode" in result.output and "was not called" in result.output


def test_local_mode_spawn_agent_with_a_cloud_model_never_builds_a_bedrock_client(
    user_layer, no_cloud
):
    """The finding's repro: an on-device turn calls spawn_agent(model='sonnet').
    With the session model also cloud (a caller that fell back to the config
    default), neither the override nor the fallback may reach Bedrock."""
    from server.agent_tools import spawn

    cloud_id = cfg.load_config()["chat_models"][_cloud_key()]
    out = asyncio.run(
        spawn.execute_spawn_agent(
            {"task": "list files", "model": "sonnet"}, session_id="", model_id=cloud_id
        )
    )
    payload = json.loads(out)
    assert payload["status"] == "failed"
    assert "Local mode" in payload["output"]
    assert "Local mode" in payload["model_warning"]


def test_local_mode_spawn_agent_override_falls_back_to_the_on_device_session_model(
    user_layer, monkeypatch
):
    from server.agent_tools import spawn
    from server.agents.runtime import AgentResult

    seen: dict = {}

    async def fake_run_agent(task, **kwargs):
        seen.update(kwargs)
        return AgentResult(agent_id="a1", agent_type="general", output="done")

    monkeypatch.setattr("server.agents.runtime.run_agent", fake_run_agent)
    out = asyncio.run(
        spawn.execute_spawn_agent(
            {"task": "list files", "model": "sonnet"}, session_id="", model_id=LOCAL_ID
        )
    )
    assert seen["model_id_override"] == LOCAL_ID
    assert "Local mode" in json.loads(out)["model_warning"]


# ── headless runs and workflow launches ──────────────────────────────────────


def test_local_mode_headless_run_is_refused_before_any_client(user_layer, no_cloud):
    from server.exec.headless import run_headless_turn
    from server.infrastructure.cloud_guard import cloud_refusal

    async def _collect():
        return [
            ev
            async for ev in run_headless_turn(
                "hey", model_key=_cloud_key(), ephemeral=True, session_id="lm-headless"
            )
        ]

    events = asyncio.run(_collect())
    assert events == [
        {"type": "error", "message": cloud_refusal("A background assistant turn")},
        {"type": "done", "status": "failed", "session_id": "lm-headless"},
    ]


def test_local_mode_workflow_default_is_on_device_or_nothing(user_layer):
    from server.workflows.routes import _default_model

    assert _default_model() == ("", "")
    key = _with_local_model(user_layer)
    assert _default_model() == (key, LOCAL_ID)
