"""The shared cloud guard: Local mode refuses every direct cloud call with one
user-facing reason, while Hybrid and Cloud pass. one_shot and the auxiliary
router consult it, so a side task never reaches Bedrock from Local mode, and the
hybrid one-shot writer set to Off never becomes a Haiku call."""

from __future__ import annotations

import pytest

from server.infrastructure import auxiliary as aux
from server.infrastructure import cloud_guard, oneshot
from server.infrastructure.model_mode import MODES


@pytest.fixture
def set_mode(monkeypatch):
    """Pin the mode on the NORMALIZED config every lazy load_config() returns."""
    from server.infrastructure import config as cfg_mod

    real = cfg_mod.load_config()

    def _set(mode: str, **extra) -> dict:
        cfg = {**real, "model_mode": mode, **extra}
        monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: cfg)
        return cfg

    return _set


@pytest.fixture
def cloud_clients(monkeypatch):
    """Record every Bedrock or mantle client a call builds."""
    built: list[str] = []

    def bedrock():
        built.append("bedrock")
        raise AssertionError("no Bedrock client may be built here")

    def mantle(*a, **k):
        built.append("mantle")
        raise AssertionError("no mantle client may be built here")

    monkeypatch.setattr("server.chat.infra._get_bedrock_client", bedrock)
    monkeypatch.setattr("server.openai_bedrock.runtime.build_sync_client", mantle)
    return built


def test_only_local_mode_refuses_and_the_reason_names_the_feature():
    for mode in MODES:
        cfg = {"model_mode": mode}
        refusal = cloud_guard.cloud_refusal("Image OCR", cfg)
        assert cloud_guard.cloud_allowed(cfg) is (refusal is None)
        assert (refusal is None) is (mode != "local")
        if refusal:
            assert "Image OCR" in refusal and "Local mode" in refusal
            with pytest.raises(cloud_guard.CloudRefused) as err:
                cloud_guard.require_cloud("Image OCR", cfg)
            assert str(err.value) == refusal
        else:
            cloud_guard.require_cloud("Image OCR", cfg)


@pytest.mark.parametrize("cloud_key", ["haiku", "gpt5.5"])
def test_one_shot_refuses_cloud_keys_in_local_mode_before_any_client(
    set_mode, cloud_clients, cloud_key
):
    set_mode("local")
    with pytest.raises(cloud_guard.CloudRefused) as err:
        oneshot.one_shot(
            "s",
            "u",
            max_tokens=8,
            engine="cloud",
            cloud_model_key=cloud_key,
            feature="Summary",
            source="condensation",
        )
    assert "Summary" in str(err.value)
    assert cloud_clients == []


def test_one_shot_local_engine_still_runs_in_local_mode(set_mode, cloud_clients, monkeypatch):
    from server.local import runtime as local_rt

    set_mode("local")
    monkeypatch.setattr(local_rt, "is_local_model", lambda k: k == "local_gemma")
    monkeypatch.setattr(local_rt, "is_downloaded", lambda k: True)
    monkeypatch.setattr(local_rt, "complete", lambda key, system, user, max_tokens: f"ran:{key}")
    out = oneshot.one_shot("s", "u", max_tokens=8, engine="local_gemma", source="condensation")
    assert out == "ran:local_gemma"
    assert cloud_clients == []


def test_one_shot_writer_off_runs_no_model(set_mode, cloud_clients, monkeypatch):
    from server.local import runtime as local_rt

    cfg = set_mode("hybrid", backends={"index_llm": oneshot.OFF})
    assert oneshot.resolve_map_engine(cfg) == oneshot.OFF

    def no_local(*a, **k):
        raise AssertionError("Off must not run an on-device model either")

    monkeypatch.setattr(local_rt, "complete", no_local)
    with pytest.raises(RuntimeError, match="Off"):
        oneshot.one_shot("s", "u", max_tokens=8, source="condensation")
    assert cloud_clients == []


def test_aux_model_id_resolves_no_cloud_model_in_local_mode():
    models = {"haiku": "h-id", "sonnet": "s-id"}
    overrides = {"auxiliary_models": {"memory_recall": "sonnet"}}
    for mode in MODES:
        cfg = {"model_mode": mode, **overrides}
        got = aux.aux_model_id("memory_recall", models=models, config=cfg, fallback_id="s-id")
        # Local mode never hands a direct Bedrock caller an id, not even the
        # caller's own fallback; every other mode resolves the configured key.
        assert (got is None) is (mode == "local")
        if got is not None:
            assert got == models["sonnet"]


def test_aux_one_shot_refuses_a_cloud_side_task_in_local_mode(set_mode, cloud_clients):
    set_mode("local", auxiliary_models={})
    with pytest.raises(cloud_guard.CloudRefused) as err:
        aux.aux_one_shot("goal_evaluator", "s", "u", max_tokens=8)
    assert "goal evaluator" in str(err.value)
    assert cloud_clients == []


def test_aux_refusal_matches_what_aux_one_shot_does(set_mode, cloud_clients, monkeypatch):
    """The up-front check and the call agree: a refusal exactly when the call
    would raise the guard's refusal, never for an on-device key."""
    from server.local import runtime as local_rt

    monkeypatch.setattr(local_rt, "is_local_model", lambda k: k == "local_gemma")
    monkeypatch.setattr(local_rt, "is_downloaded", lambda k: True)
    monkeypatch.setattr(local_rt, "complete", lambda key, system, user, max_tokens: "ok")
    for mode in MODES:
        for key in ("haiku", "local_gemma"):
            set_mode(mode, auxiliary_models={"ci_diagnose": key})
            refusal = aux.aux_refusal("ci_diagnose")
            if refusal:
                with pytest.raises(cloud_guard.CloudRefused) as err:
                    aux.aux_one_shot("ci_diagnose", "s", "u", max_tokens=8)
                assert str(err.value) == refusal
            assert (refusal is not None) is (mode == "local" and key == "haiku")
    assert cloud_clients == []
