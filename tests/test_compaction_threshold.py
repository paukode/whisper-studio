"""Per-model compaction thresholds (server/chat/compaction.thresholds_for).

The thresholds come from the model's own input budget
(server/chat/engine/windows.py). A model with no resolvable window is an
error, never a made-up default.
"""

import pytest

# ── Per-model thresholds (thresholds_for) ─────────────────────────────────────


def test_thresholds_for_uses_model_budget(monkeypatch):
    from server.chat.compaction import thresholds_for
    from server.infrastructure import config as config_mod

    cfg = {
        "chat_models": {"opus5.0": "global.anthropic.claude-opus-5"},
        "chat_model_meta": {},
    }
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    trigger, hard = thresholds_for("opus5.0")
    # A 1M-window model compacts around ~3.3M chars, far above the legacy 650K.
    assert trigger == int(872_000 * 4 * 0.95)
    assert hard == int(872_000 * 4 * 0.98)
    assert trigger < hard


def test_an_uninstalled_local_model_with_no_size_raises(monkeypatch):
    from server.chat.compaction import thresholds_for
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"chat_models": {}, "chat_model_meta": {"local_x": {"is_local": True}}},
    )
    monkeypatch.setattr("server.local.serving.resident_n_ctx", lambda: None)
    monkeypatch.setattr("server.local.runtime.requested_n_ctx", lambda: None)
    with pytest.raises(ValueError, match="not installed"):
        thresholds_for("local_x")


def test_thresholds_for_local_uses_live_n_ctx(monkeypatch):
    from server.chat.compaction import thresholds_for
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {
            "chat_models": {},
            "chat_model_meta": {"local_x": {"is_local": True, "max_output": 4096}},
        },
    )
    monkeypatch.setattr("server.local.serving.resident_n_ctx", lambda: 32_768)
    trigger, hard = thresholds_for("local_x")
    budget = 32_768 - 4096
    assert trigger == int(budget * 4 * 0.95)
    assert hard == int(budget * 4 * 0.98)
