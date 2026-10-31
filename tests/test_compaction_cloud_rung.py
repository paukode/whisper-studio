"""One selected model per compaction, no direct-provider fallback."""

import asyncio
from copy import deepcopy
from unittest.mock import Mock

import pytest

# Import before fixtures replace load_config; the provider binds it at import time.
import server.openai_bedrock.runtime  # noqa: F401
from server.chat import compaction
from server.infrastructure import config, oneshot


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    cfg = {
        "model_mode": "cloud",
        "chat_models": {
            "claude": "global.anthropic.claude-haiku-4-5",
            "gpt": "openai.gpt-5.5",
            "local_test": "local:test",
        },
        "chat_model_meta": {"gpt": {"provider": "openai_bedrock"}},
    }
    monkeypatch.setattr(config, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(compaction, "thresholds_for", lambda *a, **k: (100_000, 120_000))
    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: k == "local_test")
    return cfg


def history():
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "x" * 3000}
        for i in range(14)
    ]


def compact(messages, key):
    return asyncio.run(
        compaction.compact_messages_with_claude(
            messages, "unused-model-id", model_key=key, trigger="context-overflow"
        )
    )


@pytest.mark.parametrize("key", ["claude", "gpt", "local_test"])
def test_failure_has_one_attempt_no_other_provider(monkeypatch, key):
    summary = Mock(side_effect=RuntimeError("private provider response"))
    direct = Mock(side_effect=AssertionError("no direct fallback"))
    monkeypatch.setattr(oneshot, "one_shot", summary)
    monkeypatch.setattr("server.costs.calls.invoke_claude", direct)
    messages = history()
    original = deepcopy(messages)
    with pytest.raises(compaction.CompactionError) as error:
        compact(messages, key)
    assert "private provider" not in str(error.value)
    assert messages == original
    assert summary.call_count == 1
    assert summary.call_args.kwargs["cloud_model_key"] == key
    assert summary.call_args.kwargs["engine"] == (key if key == "local_test" else "cloud")
    direct.assert_not_called()


def test_local_mode_refuses_explicit_cloud_aux_without_local_substitution(
    isolated_models, monkeypatch
):
    isolated_models.update(model_mode="local", auxiliary_models={"compaction": "claude"})
    cloud = Mock(side_effect=AssertionError("cloud client must not be constructed"))
    local = Mock(side_effect=AssertionError("must not substitute local model"))
    monkeypatch.setattr("server.chat.infra._get_bedrock_client", cloud)
    monkeypatch.setattr("server.openai_bedrock.runtime.build_sync_client", cloud)
    monkeypatch.setattr("server.infrastructure.oneshot._local_one_shot", local)
    with pytest.raises(compaction.CompactionError):
        compact(history(), "local_test")
    cloud.assert_not_called()
    local.assert_not_called()


def test_local_mode_allows_explicit_local_aux(isolated_models, monkeypatch):
    isolated_models.update(model_mode="local", auxiliary_models={"compaction": "local_test"})
    local = Mock(return_value="Current history summary")
    monkeypatch.setattr("server.local.runtime.is_downloaded", lambda k: k == "local_test")
    monkeypatch.setattr("server.infrastructure.oneshot._local_one_shot", local)
    out = compact(history(), "claude")
    assert "Current history summary" in out[0]["content"]
    assert local.call_count == 1
    assert local.call_args.args[0] == "local_test"
