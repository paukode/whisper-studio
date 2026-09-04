"""Agents on tool-capable on-device models.

Agents used to be cloud-only: local:* was refused up front everywhere even
though the agent loop drains the same engine interactive chat runs local
models on. Now a local model that passes the registry tool gate
(supports_tools) is first-class for agents: the override resolver honours its
key, the default-model scan accepts it, and the loop serves it through the
same LocalAdapter chat uses (mark_busy/end_turn paired, retention gate
skipped so the run stays offline). Chat-only local models stay refused with a
readable reason, and structured_schema still requires a cloud model because
_distill_structured has no local branch.
"""

import asyncio
import inspect


def _cfg(models, default=None):
    cfg = {"chat_models": models}
    if default:
        cfg["default_chat_model"] = default
    return cfg


# ── override resolver ────────────────────────────────────────────────────────


def test_resolver_tool_capable_local_key_is_honored(monkeypatch):
    from server.agents.model_resolve import resolve_model_override

    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: _cfg({"qwen_big": "local:qwen-3-32b"}),
    )
    monkeypatch.setattr("server.local.runtime.supports_tools", lambda key: key == "qwen_big")
    model_id, key, warning = resolve_model_override("qwen_big", "cloud-id")
    assert model_id == "local:qwen-3-32b"
    assert key == "qwen_big"
    assert warning == ""


def test_resolver_chat_only_local_key_still_falls_back(monkeypatch):
    from server.agents.model_resolve import resolve_model_override

    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: _cfg({"tiny": "local:tiny-3b"}),
    )
    monkeypatch.setattr("server.local.runtime.supports_tools", lambda key: False)
    model_id, key, warning = resolve_model_override("tiny", "cloud-id")
    assert model_id == "cloud-id"
    assert key == ""
    assert "does not support tool calling" in warning
    assert "on-device" in warning


# ── default-model scan ───────────────────────────────────────────────────────


def test_default_scan_accepts_tool_capable_local(monkeypatch):
    from server.agents.runtime import _resolve_agent_model

    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: _cfg({"qwen_big": "local:qwen-3-32b"}, default="qwen_big"),
    )
    monkeypatch.setattr("server.agents.providers.model_key_for_id", lambda mid: "qwen_big")
    monkeypatch.setattr("server.local.runtime.supports_tools", lambda key: key == "qwen_big")
    assert _resolve_agent_model(None, config=None) == "local:qwen-3-32b"


def test_default_scan_skips_chat_only_local_for_a_cloud_entry(monkeypatch):
    from server.agents.runtime import _resolve_agent_model

    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: _cfg(
            {"tiny": "local:tiny-3b", "sonnet": "global.anthropic.claude-sonnet-5"},
            default="tiny",
        ),
    )
    monkeypatch.setattr("server.agents.providers.model_key_for_id", lambda mid: "tiny")
    monkeypatch.setattr("server.local.runtime.supports_tools", lambda key: False)
    assert _resolve_agent_model(None, config=None) == "global.anthropic.claude-sonnet-5"


def test_default_scan_returns_none_when_nothing_can_run_agents(monkeypatch):
    from server.agents.runtime import _resolve_agent_model

    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: _cfg({"tiny": "local:tiny-3b"}),
    )
    monkeypatch.setattr("server.agents.providers.model_key_for_id", lambda mid: "tiny")
    monkeypatch.setattr("server.local.runtime.supports_tools", lambda key: False)
    assert _resolve_agent_model(None, config=None) is None


# ── run_agent pre-flight gates ───────────────────────────────────────────────


def test_run_agent_refuses_a_chat_only_local_override(monkeypatch):
    from server.agents.runtime import run_agent

    monkeypatch.setattr("server.agents.providers.model_key_for_id", lambda mid: "tiny")
    monkeypatch.setattr("server.local.runtime.supports_tools", lambda key: False)
    res = asyncio.run(run_agent("t", model_id_override="local:tiny-3b"))
    assert res.status == "failed"
    assert "does not support tool calling" in res.output


def test_run_agent_refuses_structured_schema_on_local(monkeypatch):
    from server.agents.runtime import run_agent

    monkeypatch.setattr("server.agents.providers.model_key_for_id", lambda mid: "qwen_big")
    monkeypatch.setattr("server.local.runtime.supports_tools", lambda key: True)
    res = asyncio.run(
        run_agent(
            "t",
            model_id_override="local:qwen-3-32b",
            structured_schema={"type": "object"},
        )
    )
    assert res.status == "failed"
    assert "cloud" in res.output


# ── loop wiring pins ─────────────────────────────────────────────────────────


def test_local_loop_wiring_is_pinned():
    """The offline invariants ride source shape: the LocalAdapter branch, the
    paired end_turn release, the keyed-on-model-key context, the offline
    permission explainer, and the retention-gate skip."""
    from server.agents import runtime

    src_run = inspect.getsource(runtime.run_agent)
    assert "if _is_local" in src_run  # retention gate skipped for local runs

    src_loop = inspect.getsource(runtime._run_agent_loop)
    assert "LocalAdapter(" in src_loop
    assert "mark_busy=True" in src_loop
    assert "_serving.end_turn()" in src_loop
    assert 'tool_exec_model_id=("" if _is_local_agent else model_id)' in src_loop
    assert "model_id=(model_key if _is_local_agent else model_id)" in src_loop
