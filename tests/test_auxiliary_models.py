"""server.infrastructure.auxiliary: one config map routes every side task."""

from __future__ import annotations

from server.infrastructure import auxiliary as aux

MODELS = {"haiku": "h-id", "sonnet": "s-id", "local_gemma": "local:gemma"}
RICH = {"haiku": {"id": "h-id", "label": "Haiku"}, "sonnet": {"id": "s-id"}}


def test_key_resolution_defaults_and_overrides():
    assert aux.aux_model_key("title", config={}) == "haiku"
    assert aux.aux_model_key("title", config={"auxiliary_models": {"title": "sonnet"}}) == "sonnet"
    assert aux.aux_model_key("title", config={"auxiliary_models": {"title": "  "}}) == "haiku"
    assert aux.aux_model_key("title", config={"auxiliary_models": "nonsense"}) == "haiku"


def test_main_resolves_to_the_session_model(monkeypatch):
    monkeypatch.setattr(aux, "_map", lambda config=None: {"compaction": "main"})
    assert aux.resolve_task_key("compaction", "haiku", "opus5.0") == "opus5.0"
    assert aux.resolve_task_key("compaction", "haiku", None) == "haiku"
    monkeypatch.setattr(aux, "_map", lambda config=None: {})
    assert aux.resolve_task_key("compaction", "haiku", "opus5.0") == "haiku"


def test_model_id_lookup_with_models_override_and_local_refusal():
    cfg = {"auxiliary_models": {"memory_recall": "sonnet", "title": "local_gemma"}}
    assert aux.aux_model_id("memory_recall", models=MODELS, config=cfg) == "s-id"
    assert aux.aux_model_id("goal_evaluator", models=MODELS, config=cfg) == "h-id"
    # An on-device key is refused for Bedrock-only callers: default key wins.
    assert aux.aux_model_id("title", models=MODELS, config=cfg) == "h-id"
    assert aux.aux_model_id("title", models=MODELS, config=cfg, cloud_only=False) == "local:gemma"
    # Missing default falls to the caller's fallback id.
    assert (
        aux.aux_model_id("title", models={"sonnet": "s-id"}, config={}, fallback_id="s-id")
        == "s-id"
    )
    # Rich catalog entries collapse to their id.
    assert aux.aux_model_id("title", models=RICH, config={}) == "h-id"


def test_aux_one_shot_routes_cloud_and_local(monkeypatch):
    calls = []

    def fake_one_shot(system, user, *, max_tokens, engine=None, cloud_model_key="haiku", **kw):
        calls.append({"engine": engine, "key": cloud_model_key})
        return "ok"

    monkeypatch.setattr("server.infrastructure.oneshot.one_shot", fake_one_shot)
    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: k.startswith("local_"))
    monkeypatch.setattr(aux, "_map", lambda config=None: {"goal_evaluator": "sonnet"})
    assert aux.aux_one_shot("goal_evaluator", "s", "u", max_tokens=10) == "ok"
    assert calls[-1] == {"engine": "cloud", "key": "sonnet"}
    monkeypatch.setattr(aux, "_map", lambda config=None: {"goal_evaluator": "local_gemma"})
    aux.aux_one_shot("goal_evaluator", "s", "u", max_tokens=10)
    assert calls[-1]["engine"] == "local_gemma"
    monkeypatch.setattr(aux, "_map", lambda config=None: {})
    aux.aux_one_shot("goal_evaluator", "s", "u", max_tokens=10, main_model_key="opus5.0")
    assert calls[-1] == {"engine": "cloud", "key": "haiku"}


def test_goal_evaluator_asks_the_auxiliary_router(monkeypatch):
    from server.goals import evaluator

    seen = {}

    def fake(task, system, user, *, max_tokens, **kw):
        seen["task"] = task
        return '{"verdict": "achieved", "feedback": "ok", "confidence": 0.9}'

    monkeypatch.setattr(aux, "aux_one_shot", fake)
    verdict = evaluator.evaluate("goal", [{"role": "user", "content": "x"}])
    assert seen["task"] == "goal_evaluator" and verdict.is_achieved
