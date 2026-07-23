"""Data-retention pre-flight gate for agent runs.

Mythos-class models (Fable 5, requires_data_retention) reject InvokeModel when
the account isn't in provider_data_share. run_agent must fail ONE agent fast
with an actionable message instead of spawning a fan-out that each hits the raw
ValidationException.
"""

import asyncio

from server.infrastructure import data_retention as dr


def _cfg(models):
    return {"chat_models": models}


def _cfg_normalized():
    """The PRODUCTION shape: load_config() flattens chat_models to {key: id}
    and parks the rich dicts under chat_model_meta. The gate originally read
    only chat_models looking for dicts — dead code against this shape — so this
    fixture is the regression test that would have caught it."""
    return {
        "chat_models": {"fable": "fab-id", "opus": "opus-id"},
        "chat_model_meta": {
            "fable": {"requires_data_retention": True, "label": "Fable 5"},
            "opus": {"label": "Opus"},
        },
    }


def test_model_requires_data_retention_normalized_shape(monkeypatch):
    monkeypatch.setattr(
        "server.infrastructure.data_retention.load_config", lambda: _cfg_normalized()
    )
    assert dr.model_requires_data_retention("fab-id") is True
    assert dr.model_requires_data_retention("opus-id") is False
    assert dr.model_requires_data_retention("unknown") is False
    assert dr.model_requires_data_retention("") is False


def test_model_requires_data_retention_rich_shape(monkeypatch):
    # Defensive: an un-normalized rich map still matches.
    monkeypatch.setattr(
        "server.infrastructure.data_retention.load_config",
        lambda: _cfg(
            {
                "fable": {"id": "fab-id", "requires_data_retention": True},
                "opus": {"id": "opus-id"},
            }
        ),
    )
    assert dr.model_requires_data_retention("fab-id") is True
    assert dr.model_requires_data_retention("opus-id") is False


def test_block_reason_when_mode_none(monkeypatch):
    dr._reset_mode_cache()
    monkeypatch.setattr(
        "server.infrastructure.data_retention.load_config",
        lambda: _cfg({"fable": {"id": "fab-id", "requires_data_retention": True}}),
    )
    monkeypatch.setattr(dr, "get_mode", lambda: "none")
    reason = dr.retention_block_reason("fab-id")
    assert reason and "provider_data_share" in reason


def test_no_block_when_sharing_on(monkeypatch):
    dr._reset_mode_cache()
    monkeypatch.setattr(
        "server.infrastructure.data_retention.load_config",
        lambda: _cfg({"fable": {"id": "fab-id", "requires_data_retention": True}}),
    )
    monkeypatch.setattr(dr, "get_mode", lambda: dr.SHARING_MODE)
    assert dr.retention_block_reason("fab-id") is None


def test_no_block_for_non_retention_model(monkeypatch):
    dr._reset_mode_cache()
    monkeypatch.setattr(
        "server.infrastructure.data_retention.load_config",
        lambda: _cfg({"opus": {"id": "opus-id"}}),
    )

    def _explode():
        raise AssertionError("must not query account mode for a non-retention model")

    monkeypatch.setattr(dr, "get_mode", _explode)
    assert dr.retention_block_reason("opus-id") is None


def test_fail_open_when_mode_unknown(monkeypatch):
    dr._reset_mode_cache()
    monkeypatch.setattr(
        "server.infrastructure.data_retention.load_config",
        lambda: _cfg({"fable": {"id": "fab-id", "requires_data_retention": True}}),
    )

    def _boom():
        raise RuntimeError("missing bedrock:GetAccountDataRetention")

    monkeypatch.setattr(dr, "get_mode", _boom)
    # Unknown mode must NOT block (fail open) — the invoke surfaces any real error.
    assert dr.retention_block_reason("fab-id") is None


def test_mode_cache_avoids_repeated_calls(monkeypatch):
    dr._reset_mode_cache()
    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return "none"

    monkeypatch.setattr(dr, "get_mode", _counting)
    assert dr.get_mode_cached() == "none"
    assert dr.get_mode_cached() == "none"
    assert calls["n"] == 1  # second read served from cache


def test_failures_are_negative_cached(monkeypatch):
    # A failing control-plane must not be re-queried per agent in a fan-out:
    # the failure is cached (shorter TTL) and served as None until it expires.
    dr._reset_mode_cache()
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(dr, "get_mode", _boom)
    assert dr.get_mode_cached() is None
    assert dr.get_mode_cached() is None
    assert calls["n"] == 1  # second failure served from the negative cache


def test_set_enabled_refreshes_cache(monkeypatch):
    # After the user consents, the gate must see the new mode immediately —
    # not the stale cached one for up to the TTL.
    dr._reset_mode_cache()
    monkeypatch.setattr(dr, "get_mode", lambda: "none")
    assert dr.get_mode_cached() == "none"

    class _FakeCP:
        def put_account_data_retention(self, mode):
            self.mode = mode

    monkeypatch.setattr(dr, "_get_control_plane_client", lambda: _FakeCP())
    assert dr.set_enabled(True) == dr.SHARING_MODE
    # No get_mode call needed: the cache was refreshed by set_enabled itself.
    monkeypatch.setattr(dr, "get_mode", lambda: (_ for _ in ()).throw(AssertionError("hit CP")))
    assert dr.get_mode_cached() == dr.SHARING_MODE


def test_run_agent_blocks_early(monkeypatch):
    # Gate returns a reason -> run_agent short-circuits (status failed) and never
    # reaches the provider.
    monkeypatch.setattr(
        "server.infrastructure.data_retention.retention_block_reason",
        lambda model_id: "needs retention",
    )
    monkeypatch.setattr("server.agents.runtime._resolve_agent_model", lambda o, c: "fab-id")
    from server.agents.runtime import run_agent

    res = asyncio.run(
        run_agent("do it", agent_type="general", session_id="", model_id_override="fab-id")
    )
    assert res.status == "failed"
    assert "data retention" in res.output.lower()
