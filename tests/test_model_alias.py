"""spawn_agent's model override accepts a family alias ("sonnet") and resolves
it to the newest configured key in that family, with a note in the warning
slot so the override still never fails silently. Before, 'sonnet' was
"unknown model" and quietly ran on the default model (14 times in one log)."""

from server.agents.model_resolve import resolve_model_override

_MODELS = {
    "sonnet4.5": "global.anthropic.claude-sonnet-4-5",
    "sonnet5.0": "global.anthropic.claude-sonnet-5",
    "opus5.0": "global.anthropic.claude-opus-5",
    "haiku": "global.anthropic.claude-haiku-4-5",
}


def _patch(monkeypatch):
    monkeypatch.setattr(
        "server.infrastructure.config.load_config", lambda *a, **k: {"chat_models": dict(_MODELS)}
    )


def test_family_alias_resolves_to_the_newest_version(monkeypatch):
    _patch(monkeypatch)
    model_id, key, note = resolve_model_override("sonnet", "default-id")
    assert model_id == "global.anthropic.claude-sonnet-5"
    assert key == "sonnet5.0"
    assert "alias" in note and "sonnet5.0" in note


def test_alias_is_case_insensitive_and_exact_key_still_wins(monkeypatch):
    _patch(monkeypatch)
    assert resolve_model_override("Opus", "d")[1] == "opus5.0"
    model_id, key, note = resolve_model_override("sonnet4.5", "d")
    assert (model_id, key, note) == ("global.anthropic.claude-sonnet-4-5", "sonnet4.5", "")


def test_truly_unknown_key_still_degrades_with_a_warning(monkeypatch):
    _patch(monkeypatch)
    model_id, key, note = resolve_model_override("gemini", "default-id")
    assert (model_id, key) == ("default-id", "")
    assert "unknown model 'gemini'" in note
