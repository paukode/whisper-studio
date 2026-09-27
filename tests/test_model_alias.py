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


def test_version_ties_go_to_the_first_listed_key(monkeypatch):
    # "gpt6-astra", "gpt6-sol" and "gpt6-luna" carry no trailing version, so
    # they tie. The first one listed (the flagship) wins, however many smaller
    # siblings are appended after it; the last one used to win, which turned
    # "gpt6" from Astra into Luna the day Luna was added.
    models = {
        "gpt6-astra": "openai.gpt-6-astra",
        "gpt6-sol": "openai.gpt-6-sol",
        "gpt6-luna": "openai.gpt-6-luna",
    }
    monkeypatch.setattr(
        "server.infrastructure.config.load_config", lambda *a, **k: {"chat_models": models}
    )
    model_id, key, note = resolve_model_override("gpt6", "default-id")
    assert (model_id, key) == ("openai.gpt-6-astra", "gpt6-astra")
    assert "gpt6-astra" in note


def test_shipped_catalog_lists_the_priciest_key_first_on_a_version_tie():
    # The tie rule above leans on catalog order, so every alias whose newest
    # version is shared by several shipped cloud keys must list the priciest
    # (flagship) of them first. Otherwise an agent asking for the family gets
    # a smaller model than the family's best without anyone choosing that.
    from server.agents.model_resolve import _version_sort_key
    from server.costs.tracker import get_model_pricing
    from server.infrastructure.config import load_config

    models = {
        k: v
        for k, v in (load_config().get("chat_models") or {}).items()
        if not str(v).startswith("local:") and get_model_pricing(k)
    }

    def price(k):
        p = get_model_pricing(k)
        return p["input"] + p["output"]

    aliases = {k[:n].lower() for k in models for n in range(1, len(k) + 1)}
    for alias in sorted(aliases - {k.lower() for k in models}):
        family = [k for k in models if k.lower().startswith(alias)]
        top = max(_version_sort_key(k) for k in family)
        tied = [k for k in family if _version_sort_key(k) == top]
        assert price(tied[0]) == max(price(k) for k in tied), (alias, tied)
        assert resolve_model_override(alias, "d")[1] == tied[0], alias


def test_a_family_alias_reaches_a_version_that_sits_before_a_suffix(monkeypatch):
    # The GPT keys carry their version before the model name ("gpt6-astra",
    # "gpt5.6-sol"), so a version read only at the end of the key ranked them
    # all unversioned and "gpt" went to the older GPT-5.5.
    models = {
        "opus5.5": "global.anthropic.claude-opus-5-5",
        "opus5.0": "global.anthropic.claude-opus-5",
        "gpt6-astra": "openai.gpt-6-astra",
        "gpt6-sol": "openai.gpt-6-sol",
        "gpt5.6-sol": "openai.gpt-5.6-sol",
        "gpt5.6-luna": "openai.gpt-5.6-luna",
        "gpt5.5": "openai.gpt-5.5",
        "gpt5.4": "openai.gpt-5.4",
        "sonnet5": "global.anthropic.claude-sonnet-5",
        "sonnet": "global.anthropic.claude-sonnet-4-6",
    }
    monkeypatch.setattr(
        "server.infrastructure.config.load_config", lambda *a, **k: {"chat_models": models}
    )
    aliases = ("gpt", "gpt5", "gpt5.6", "gpt6", "opus", "son")
    resolved = {alias: resolve_model_override(alias, "d")[1] for alias in aliases}
    assert resolved == {
        "gpt": "gpt6-astra",
        "gpt5": "gpt5.6-sol",
        "gpt5.6": "gpt5.6-sol",
        "gpt6": "gpt6-astra",
        "opus": "opus5.5",
        "son": "sonnet5",
    }
