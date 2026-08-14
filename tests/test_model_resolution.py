"""Audit items #5 (remainder) and #14 (remainder).

- #5: provider selection re-read the GLOBAL config after the turn had already
  latched a workspace-aware one, so a model defined only in a workspace's
  .whisper/settings.json defaulted to "anthropic" — sending an OpenAI model
  id through the Anthropic adapter.
- #14: a workflow script's opts.model override that could not be honoured
  (unknown key, or on-device with no agent adapter) fell back silently, and
  the ledger then priced the work under the name that never ran.
"""

import inspect

# ── #5: provider selection uses the latched, workspace-aware metadata ───────


def test_provider_selection_uses_the_latched_meta_not_a_global_lookup():
    from server.chat import routes

    src = inspect.getsource(routes.chat_endpoint)
    # The adapter branch must read the same _model_meta the effort was
    # resolved from, never a fresh _get_chat_model_meta() lookup.
    assert '_provider = _model_meta.get("provider"' in src
    assert '_get_chat_model_meta().get(model_key, {}).get("provider"' not in src


# ── #14: workflow model overrides are validated, not silently swallowed ─────


def _resolve(opts, default_id="global.anthropic.claude-opus-5", models=None, monkeypatch=None):
    from server.workflows import agent_adapter

    cfg = {
        "chat_models": models
        if models is not None
        else {"sonnet": "global.anthropic.claude-sonnet-5"}
    }
    monkeypatch.setattr("server.infrastructure.config.load_config", lambda *a, **k: cfg)
    return agent_adapter._resolve_model(opts, default_id)


def test_known_cloud_override_resolves_and_reports_its_key(monkeypatch):
    model_id, key, warning = _resolve({"model": "sonnet"}, monkeypatch=monkeypatch)
    assert model_id == "global.anthropic.claude-sonnet-5"
    assert key == "sonnet"
    assert warning == ""


def test_no_override_uses_the_runs_model_with_no_warning(monkeypatch):
    model_id, key, warning = _resolve({}, monkeypatch=monkeypatch)
    assert model_id == "global.anthropic.claude-opus-5"
    assert key == ""  # empty ⇒ the ledger falls back to the run's own key
    assert warning == ""


def test_unknown_override_warns_and_does_not_claim_the_bad_key(monkeypatch):
    model_id, key, warning = _resolve({"model": "does-not-exist"}, monkeypatch=monkeypatch)
    assert model_id == "global.anthropic.claude-opus-5"  # fell back
    assert key == ""  # the ledger must NOT price under the invalid name
    assert "unknown model" in warning


def test_local_override_is_refused_with_a_readable_reason(monkeypatch):
    model_id, key, warning = _resolve(
        {"model": "local_gemma"},
        models={"local_gemma": "local:gemma-4-12b", "sonnet": "global.anthropic.claude-sonnet-5"},
        monkeypatch=monkeypatch,
    )
    # Workflow agents have no local adapter — must not hand a local: id to
    # the Bedrock adapter and fail at invoke.
    assert model_id == "global.anthropic.claude-opus-5"
    assert key == ""
    assert "on-device" in warning


def test_ledger_prices_the_model_that_actually_ran():
    """The mispricing bug: the ledger used opts.model even when that override
    was rejected, charging real work to a model that never ran."""
    from server.workflows.runtime import WorkflowRun

    src = inspect.getsource(WorkflowRun._handle_agent)
    assert 'result.get("model_key") or self.model_key' in src
    assert 'model_key=opts.get("model")' not in src
