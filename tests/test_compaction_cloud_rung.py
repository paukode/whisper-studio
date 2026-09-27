"""Compaction's last-resort direct InvokeModel rung only ever takes a Claude
Bedrock id outside Local mode. An on-device session (whose model id is its
local key) or a GPT session goes straight to truncation when its one-shot
summary fails, so the conversation is never sent to Bedrock under a model id
that cannot take it, and Local mode never summarises on a cloud key."""

from __future__ import annotations

import asyncio

import pytest

from server.chat import compaction, compaction_log


@pytest.fixture(autouse=True)
def _quiet(tmp_path, monkeypatch):
    monkeypatch.setattr(compaction_log, "data_root", lambda: str(tmp_path))
    # Force the ladder past rung 0 and skip the session-memory shortcut.
    monkeypatch.setattr(compaction, "thresholds_for", lambda *_a, **_k: (10, 20))


@pytest.fixture
def set_mode(monkeypatch):
    from server.infrastructure import config as cfg_mod

    real = cfg_mod.load_config()

    def _set(mode: str, **extra) -> None:
        cfg = {**real, "model_mode": mode, **extra}
        monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: cfg)

    return _set


@pytest.fixture
def invokes(monkeypatch):
    """Every direct Bedrock InvokeModel the rung makes, by modelId."""
    calls: list[str] = []

    class _Client:
        def invoke_model(self, *, modelId, **_kw):  # noqa: N803 - boto3 kwarg
            calls.append(modelId)
            raise RuntimeError("stub: no network in tests")

    monkeypatch.setattr(compaction, "_get_bedrock_client", lambda: _Client())
    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: k.startswith("local_"))
    return calls


def _failing_one_shot(seen: list[dict]):
    def fake(system, user, *, max_tokens, engine=None, cloud_model_key="haiku", **kw):
        seen.append({"engine": engine, "cloud_model_key": cloud_model_key})
        raise RuntimeError("summarizer returned empty")

    return fake


def _history(n: int = 14) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "x" * 300}
        for i in range(n)
    ]


def _compact(model_id: str, model_key: str) -> list:
    return asyncio.run(
        compaction.compact_messages_with_claude(
            _history(), model_id, session_id="", model_key=model_key, trigger="context-overflow"
        )
    )


@pytest.mark.parametrize(
    ("model_id", "model_key"),
    [("local_gemma", "local_gemma"), ("openai.gpt-5.5", "gpt5.5")],
)
def test_failed_summary_never_sends_a_non_claude_id_to_invoke_model(
    set_mode, invokes, monkeypatch, model_id, model_key
):
    set_mode("cloud")
    seen: list[dict] = []
    monkeypatch.setattr("server.infrastructure.oneshot.one_shot", _failing_one_shot(seen))
    out = _compact(model_id, model_key)
    assert seen, "the provider-aware summarizer is still tried first"
    assert invokes == []
    assert out and len(out) < len(_history())  # truncation still shrinks the history


def test_claude_session_keeps_its_direct_rung_outside_local_mode(set_mode, invokes, monkeypatch):
    set_mode("cloud")
    monkeypatch.setattr("server.infrastructure.oneshot.one_shot", _failing_one_shot([]))
    model_id = "global.anthropic.claude-opus-5"
    _compact(model_id, "opus5.0")
    assert invokes == [model_id]


def test_local_mode_summarises_on_the_session_model_not_a_cloud_aux_key(
    set_mode, invokes, monkeypatch
):
    set_mode("local", auxiliary_models={"compaction": "haiku"})
    seen: list[dict] = []
    monkeypatch.setattr("server.infrastructure.oneshot.one_shot", _failing_one_shot(seen))
    _compact("global.anthropic.claude-opus-5", "local_gemma")
    # The on-device session model runs the summary; the cloud aux key never does.
    assert [c["engine"] for c in seen] == ["local_gemma"]
    assert all(c["cloud_model_key"] != "haiku" or c["engine"] != "cloud" for c in seen)
    # And Local mode never opens the direct rung, even for a Claude-looking id.
    assert invokes == []
