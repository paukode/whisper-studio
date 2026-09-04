"""spawn_agent ``model`` override.

The param used to exist only as an informational hint inside agent_definition
that never selected anything. It now resolves through the same
validated-never-silent path as a workflow agent() override
(server/agents/model_resolve.py): a known cloud key switches the child's
model, an unknown or on-device key falls back to the session model with a
warning in the tool result, and the ledger prices the model that actually ran.
"""

import asyncio
import json


def _cfg(models=None):
    return {
        "chat_models": models
        if models is not None
        else {
            "haiku": "global.anthropic.claude-haiku-4-5",
            "sonnet": "global.anthropic.claude-sonnet-5",
        }
    }


# ── shared resolver ──────────────────────────────────────────────────────────


def test_resolver_known_key_switches_model(monkeypatch):
    from server.agents.model_resolve import resolve_model_override

    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: _cfg())
    model_id, key, warning = resolve_model_override("haiku", "session-model-id")
    assert model_id == "global.anthropic.claude-haiku-4-5"
    assert key == "haiku"
    assert warning == ""


def test_resolver_no_key_inherits_default(monkeypatch):
    from server.agents.model_resolve import resolve_model_override

    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: _cfg())
    model_id, key, warning = resolve_model_override(None, "session-model-id")
    assert model_id == "session-model-id"
    assert key == "" and warning == ""


def test_resolver_unknown_key_falls_back_with_warning(monkeypatch):
    from server.agents.model_resolve import resolve_model_override

    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: _cfg())
    model_id, key, warning = resolve_model_override("nope", "session-model-id")
    assert model_id == "session-model-id"
    assert key == ""  # ledger must not price under the invalid name
    assert "unknown model" in warning


def test_resolver_local_key_is_refused(monkeypatch):
    from server.agents.model_resolve import resolve_model_override

    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: _cfg({"local_gemma": "local:gemma-4-12b"}),
    )
    model_id, key, warning = resolve_model_override("local_gemma", "session-model-id")
    assert model_id == "session-model-id"
    assert key == ""
    assert "on-device" in warning


# ── spawn_agent threads the resolution through ───────────────────────────────


def _spawn(monkeypatch, tool_input, models=None):
    from server.agent_tools import spawn
    from server.agents.runtime import AgentResult

    seen = {}

    async def fake_run_agent(task, **kwargs):
        seen.update(kwargs)
        return AgentResult(agent_id="a1", agent_type="general", output="done")

    monkeypatch.setattr("server.agents.runtime.run_agent", fake_run_agent)
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: _cfg(models))
    out = asyncio.run(
        spawn.execute_spawn_agent(tool_input, session_id="", model_id="session-model-id")
    )
    return seen, json.loads(out)


def test_spawn_override_reaches_run_agent_and_result(monkeypatch):
    seen, payload = _spawn(monkeypatch, {"task": "t", "model": "haiku"})
    assert seen["model_id_override"] == "global.anthropic.claude-haiku-4-5"
    assert payload["model_key"] == "haiku"
    assert "model_warning" not in payload


def test_spawn_unknown_override_falls_back_and_warns_in_result(monkeypatch):
    seen, payload = _spawn(monkeypatch, {"task": "t", "model": "nope"})
    assert seen["model_id_override"] == "session-model-id"
    assert payload["model_key"] is None
    assert "unknown model" in payload["model_warning"]


def test_spawn_without_override_keeps_session_model_and_quiet_result(monkeypatch):
    seen, payload = _spawn(monkeypatch, {"task": "t"})
    assert seen["model_id_override"] == "session-model-id"
    assert "model_key" not in payload
    assert "model_warning" not in payload
