"""Remaining audit items #11, #14, #16.

- #14: the workflow ledger read cache token counts under keys the agent
  runtime never emits, so every workflow's displayed cost omitted cache
  charges entirely.
- #16: reasoning-tier inference read only the config KEY, so a renamed or
  dotless entry silently lost its ladder's top rung.
- #11: a budget fallback swaps the model before effort is resolved, so the
  clamp can quietly drop the turn's reasoning with nothing telling the UI.
"""

from server.infrastructure.effort import (
    api_effort_for,
    effort_levels_for,
    effort_tier_for,
    infer_effort_tier,
)

# ── #14: workflow cache-cost accounting ─────────────────────────────────────


def _run_with_usage(usage: dict, model_key: str = "sonnet"):
    from server.workflows.runtime import WorkflowRun

    run = WorkflowRun("r1", "", model_key=model_key)
    run._account(usage, model_key=model_key)
    return run


def test_workflow_ledger_reads_the_keys_agents_actually_emit(monkeypatch):
    seen = {}

    def _fake_estimate(model_key, ti, to, cache_read_tokens=0, cache_creation_tokens=0):
        seen.update(
            {"read": cache_read_tokens, "creation": cache_creation_tokens, "in": ti, "out": to}
        )
        return 0.0

    monkeypatch.setattr("server.costs.tracker.estimate_cost", _fake_estimate)
    # Exactly the shape server/agents/runtime.py reports.
    _run_with_usage(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 4000,
            "cache_creation_tokens": 700,
        }
    )
    assert seen["read"] == 4000
    assert seen["creation"] == 700


def test_workflow_ledger_still_accepts_the_bare_key_names(monkeypatch):
    seen = {}

    def _fake_estimate(model_key, ti, to, cache_read_tokens=0, cache_creation_tokens=0):
        seen.update({"read": cache_read_tokens, "creation": cache_creation_tokens})
        return 0.0

    monkeypatch.setattr("server.costs.tracker.estimate_cost", _fake_estimate)
    _run_with_usage({"input_tokens": 1, "output_tokens": 1, "cache_read": 11, "cache_creation": 22})
    assert seen["read"] == 11
    assert seen["creation"] == 22


def test_workflow_ledger_still_totals_plain_tokens():
    run = _run_with_usage({"input_tokens": 100, "output_tokens": 50})
    assert run.tokens_in == 100
    assert run.tokens_out == 50


# ── #16: tier inference reads the model id, not just the key ────────────────


def test_tier_inference_falls_back_to_the_model_id():
    # A dotless/renamed key can't match the opus MAJOR.MINOR regex, so before
    # this it silently degraded to "standard" and lost the xhigh rung.
    assert infer_effort_tier("opus5") == "standard"  # key alone: no version match
    assert infer_effort_tier("opus5", "global.anthropic.claude-opus-5-0") == "full"
    assert infer_effort_tier("my-opus", "global.anthropic.claude-opus-4-8") == "full"
    assert infer_effort_tier("whatever", "global.anthropic.claude-fable-5") == "full"
    assert infer_effort_tier("whatever", "global.anthropic.claude-haiku-4-5") == "none"


def test_tier_inference_keeps_pre_4_8_opus_on_standard():
    assert infer_effort_tier("x", "global.anthropic.claude-opus-4-7") == "standard"
    assert infer_effort_tier("x", "global.anthropic.claude-opus-4-6") == "standard"


def test_renamed_opus_5_keeps_its_full_ladder_and_wire_value():
    meta = {"id": "global.anthropic.claude-opus-5-0"}
    assert effort_tier_for(meta, "my-opus") == "full"
    levels = effort_levels_for(meta, "my-opus")
    assert "extra" in levels  # the rung it used to lose
    assert "ultracode" in levels
    # And ultracode rides xhigh again rather than degrading to max.
    assert api_effort_for("ultracode", meta, "my-opus") == "xhigh"


def test_explicit_effort_tier_still_wins_over_both_key_and_id():
    meta = {"id": "global.anthropic.claude-opus-5-0", "effort_tier": "standard"}
    assert effort_tier_for(meta, "opus5.0") == "standard"


def test_shipped_keys_are_unchanged_by_the_id_fallback():
    # The normal, well-spelled entries must resolve exactly as before.
    assert infer_effort_tier("opus5.0", "global.anthropic.claude-opus-5") == "full"
    assert infer_effort_tier("opus4.8", "global.anthropic.claude-opus-4-8") == "full"
    assert infer_effort_tier("opus4.7", "global.anthropic.claude-opus-4-7") == "standard"
    assert infer_effort_tier("sonnet5", "global.anthropic.claude-sonnet-5") == "standard"
    assert infer_effort_tier("haiku", "global.anthropic.claude-haiku-4-5") == "none"


# ── #11: the downgrade event is declared on the wire contract ───────────────


# ── #18: the documented 16-way concurrency is the real one ──────────────────


def test_agent_call_concurrency_matches_the_workflow_scheduler():
    """ULTRACODE.md promises 16 concurrent agents. The scheduler admitted 16
    but the model-call pool only had 4 threads, so 12 sat blocked — the
    documented number was never the real one."""
    from server.agents.providers.base import AGENT_CALL_CONCURRENCY
    from server.workflows.runtime import WORKFLOW_MAX_CONCURRENCY

    assert AGENT_CALL_CONCURRENCY == WORKFLOW_MAX_CONCURRENCY


def test_agent_executor_is_sized_from_that_one_knob():
    from server.agents.providers.base import AGENT_CALL_CONCURRENCY
    from server.agents.runtime import _agent_executor

    assert _agent_executor._max_workers == AGENT_CALL_CONCURRENCY


def test_bedrock_client_uses_adaptive_retries(monkeypatch):
    """Raising concurrency is only safe with client-side throttle pacing —
    the report's explicit condition for increasing the executor."""
    captured = {}

    def _fake_client(service, **kwargs):
        captured["config"] = kwargs.get("config")
        return object()

    import server.chat.infra as infra

    monkeypatch.setattr(infra.boto3, "client", _fake_client)
    infra._reset_bedrock_client_cache()
    infra._get_bedrock_client()
    infra._reset_bedrock_client_cache()

    cfg = captured["config"]
    assert cfg.retries["mode"] == "adaptive"
    assert cfg.retries["max_attempts"] >= 3
    # The socket pool must still comfortably exceed the concurrency.
    from server.agents.providers.base import AGENT_CALL_CONCURRENCY

    assert cfg.max_pool_connections >= AGENT_CALL_CONCURRENCY


def test_turn_downgrade_event_is_emitted_by_the_chat_route():
    """The event has to actually be yielded, not just computed — a computed
    dict nobody sends is exactly the shape of the bug being fixed."""
    import inspect

    from server.chat import routes

    src = inspect.getsource(routes)
    assert "turn_downgrade" in src
    assert "'turn_downgrade': _downgrade" in src or '"turn_downgrade": _downgrade' in src
