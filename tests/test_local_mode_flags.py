"""A feature flag whose feature has no on-device path is off in Local mode.

Local mode promises that nothing leaves this Mac, so a cloud-only feature
(``rag_query_rewrite`` rewrites a follow-up on a cloud model) stays off there
whatever its flag says. It is not a silent no-op: the flag's state carries the
reason, which the Feature flags panel shows next to the switch.

The user layer is patched where production reads it
(``config._load_user_config``), so the real load_config runs on top.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.infrastructure import config as cfg
from server.infrastructure.feature_flags import get_flag_states, is_enabled


@pytest.fixture
def user_layer(monkeypatch):
    layer: dict = {"feature_flags": {"rag_query_rewrite": True}}
    monkeypatch.setattr(cfg, "_load_user_config", lambda: json.loads(json.dumps(layer)))
    cfg._invalidate_cache()
    yield layer
    cfg._invalidate_cache()


def _set_mode(layer: dict, mode: str) -> None:
    layer["model_mode"] = mode
    cfg._invalidate_cache()


def test_local_mode_turns_a_cloud_only_flag_off_and_says_why(user_layer):
    _set_mode(user_layer, "local")
    assert is_enabled("rag_query_rewrite") is False
    state = get_flag_states()["rag_query_rewrite"]
    # The switch still shows what the user set; the reason says it has no effect.
    assert state["enabled"] is True
    assert "Local mode" in state["inactive_reason"]


@pytest.mark.parametrize("mode", ["hybrid", "cloud"])
def test_other_modes_leave_the_flag_in_charge(user_layer, mode):
    _set_mode(user_layer, mode)
    assert is_enabled("rag_query_rewrite") is True
    assert get_flag_states()["rag_query_rewrite"]["inactive_reason"] is None


def test_a_flag_that_runs_on_device_is_never_marked_inactive(user_layer):
    _set_mode(user_layer, "local")
    states = get_flag_states()
    assert states["rag_hybrid_search"]["inactive_reason"] is None
    assert is_enabled("rag_hybrid_search") is states["rag_hybrid_search"]["enabled"]


def test_local_mode_grounding_never_asks_the_cloud_to_rewrite(user_layer, monkeypatch):
    from server.chat import routes

    _set_mode(user_layer, "local")

    async def _no_rewrite(*_a, **_k):
        raise AssertionError("query rewrite ran in Local mode")

    seen: dict = {}

    def _retrieve(indexes, query, **kw):
        seen["query"], seen["extra"] = query, kw.get("extra_queries")
        return "", {}

    monkeypatch.setattr(routes, "_rewrite_query_for_retrieval", _no_rewrite)
    monkeypatch.setattr("server.index.pipeline.retrieve_grounding", _retrieve)
    history = [{"role": "user", "content": "tell me about the Q3 report"}]
    asyncio.run(routes._run_grounding(["docs"], "and its risks?", history, False))
    # The heuristic (Tier 1+2) path ran with the raw question as the primary query.
    assert seen["query"] == "and its risks?"


def test_local_mode_turns_dream_consolidation_off_and_says_why(user_layer):
    _set_mode(user_layer, "local")
    assert is_enabled("dream_consolidation") is False
    assert "Local mode" in get_flag_states()["dream_consolidation"]["inactive_reason"]


def test_local_mode_keeps_memory_recall_and_says_recording_is_off(user_layer):
    """Recall is local file reads and keeps working; recording new memories
    runs on a cloud model, and the flag's state says so instead of the Memory
    row reading plain On."""
    _set_mode(user_layer, "local")
    state = get_flag_states()["auto_memory"]
    assert is_enabled("auto_memory") is True and state["inactive_reason"] is None
    assert "records no new ones" in state["local_mode_note"]
    _set_mode(user_layer, "hybrid")
    assert get_flag_states()["auto_memory"]["local_mode_note"] is None
