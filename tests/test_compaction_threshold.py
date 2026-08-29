"""Constant-sanity checks for the compaction thresholds in
server/chat/compaction.py.

The chat loop compacts proactively when the estimated message size exceeds
COMPACT_TRIGGER_CHARS, before a request would hit Bedrock's 200K-token input
cap (~800K chars at the codebase's 4-chars/token estimate). These tests pin
both thresholds below that cap so
proactive compaction actually fires, instead of every long conversation
falling through to a billed PromptTooLongError.
"""

from server.chat.compaction import COMPACT_TRIGGER_CHARS, MAX_CONTEXT_CHARS

# Bedrock input cap: 200K tokens * 4 chars/token.
BEDROCK_INPUT_CAP_CHARS = 800_000


def test_compact_trigger_below_bedrock_cap():
    # The whole point of the fix: the proactive trigger sits below the cap so
    # compaction fires before the reactive PromptTooLongError path.
    assert 0 < COMPACT_TRIGGER_CHARS < BEDROCK_INPUT_CAP_CHARS


def test_max_context_below_bedrock_cap():
    # The simple-truncation fallback trims down to MAX_CONTEXT_CHARS, so it too
    # must stay under the cap or the fallback itself would overflow the request.
    assert 0 < MAX_CONTEXT_CHARS <= BEDROCK_INPUT_CAP_CHARS


def test_hard_ceiling_at_or_above_trigger():
    # Today's invariant: MAX_CONTEXT_CHARS (the last-resort truncation ceiling)
    # sits at or above COMPACT_TRIGGER_CHARS (the proactive trigger). Compaction
    # is attempted at the trigger; the ceiling is the safety net nearer the cap.
    assert MAX_CONTEXT_CHARS >= COMPACT_TRIGGER_CHARS


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
    assert COMPACT_TRIGGER_CHARS < trigger < hard


def test_thresholds_for_falls_back_to_legacy_constants(monkeypatch):
    from server.chat.compaction import thresholds_for
    from server.infrastructure import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"chat_models": {}, "chat_model_meta": {"local_x": {"is_local": True}}},
    )
    monkeypatch.setattr("server.local.serving.resident_n_ctx", lambda: None)
    monkeypatch.setattr("server.local.runtime.requested_n_ctx", lambda: None)
    # Unsized local model and no key at all both land on the legacy constants.
    assert thresholds_for("local_x") == (COMPACT_TRIGGER_CHARS, MAX_CONTEXT_CHARS)
    assert thresholds_for(None) == (COMPACT_TRIGGER_CHARS, MAX_CONTEXT_CHARS)


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
