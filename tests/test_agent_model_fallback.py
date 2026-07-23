"""Fallback model resolution must never hand an agent an on-device model.

get_adapter has no local branch, so a ``local:*`` id resolved from
default_chat_model routes to the Bedrock adapter and fails at invoke. That
silently broke background memory agents (extraction claims its cursor slice
before the run, so skipped messages leave no visible symptom) in hybrid mode
whenever the user's default chat model was local. Fallback resolution must
skip local entries; when every configured chat model is local, run_agent must
fail early instead of erroring at the provider call.
"""

import asyncio
import dataclasses

from server.agents.config import AGENT_TYPES
from server.agents.runtime import _resolve_agent_model, run_agent
from server.infrastructure import config as config_mod

LOCAL_GEMMA = "local:gemma-4-12b-it-qat-q4_0"
LOCAL_CODER = "local:gemma-4-12b-coder"
CLOUD_SONNET = "us.anthropic.claude-sonnet-5-v1:0"
CLOUD_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

GENERAL = AGENT_TYPES["general"]


def _patch_config(monkeypatch, chat_models, default_chat_model=None):
    cfg = {"chat_models": chat_models, "default_chat_model": default_chat_model}
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)


def test_local_default_falls_back_to_non_local(monkeypatch):
    _patch_config(
        monkeypatch,
        {"local_gemma": LOCAL_GEMMA, "sonnet": CLOUD_SONNET},
        default_chat_model="local_gemma",
    )
    assert _resolve_agent_model(None, GENERAL) == CLOUD_SONNET


def test_local_default_without_sonnet_picks_first_non_local(monkeypatch):
    _patch_config(
        monkeypatch,
        {"local_gemma": LOCAL_GEMMA, "haiku": CLOUD_HAIKU},
        default_chat_model="local_gemma",
    )
    assert _resolve_agent_model(None, GENERAL) == CLOUD_HAIKU


def test_local_config_model_is_skipped(monkeypatch):
    _patch_config(
        monkeypatch,
        {"local_gemma": LOCAL_GEMMA, "sonnet": CLOUD_SONNET},
        default_chat_model="sonnet",
    )
    cfg = dataclasses.replace(GENERAL, model="local_gemma")
    assert _resolve_agent_model(None, cfg) == CLOUD_SONNET


def test_explicit_override_is_returned_verbatim(monkeypatch):
    # Callers that pass model_id_override own that choice; resolution must not
    # second-guess it (not even for a local id — the chat spawn path never
    # sends one, and rewriting overrides would mask caller bugs).
    _patch_config(monkeypatch, {"sonnet": CLOUD_SONNET}, default_chat_model="sonnet")
    assert _resolve_agent_model(CLOUD_HAIKU, GENERAL) == CLOUD_HAIKU
    assert _resolve_agent_model(LOCAL_GEMMA, GENERAL) == LOCAL_GEMMA


def test_all_local_config_resolves_to_none(monkeypatch):
    _patch_config(
        monkeypatch,
        {"local_gemma": LOCAL_GEMMA, "local_gemma_coder": LOCAL_CODER},
        default_chat_model="local_gemma",
    )
    assert _resolve_agent_model(None, GENERAL) is None


def test_run_agent_fails_early_when_all_models_local(monkeypatch):
    _patch_config(
        monkeypatch,
        {"local_gemma": LOCAL_GEMMA, "local_gemma_coder": LOCAL_CODER},
        default_chat_model="local_gemma",
    )
    result = asyncio.run(run_agent("extract memories", session_id="test-session"))
    assert result.status == "failed"
    assert result.agent_id == ""
    assert "on-device" in result.output
