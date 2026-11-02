"""pricing.json in the config folder overrides pricing.example.json per key."""

import json

from server.costs import tracker


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return str(path)


def _entry(raw):
    return tracker._coerce_pricing_entry(raw)


def test_an_override_replaces_only_its_own_key(tmp_path, monkeypatch):
    # For example GPT set to $0 while AWS bills it at $0.
    default_astra = {"input": 11.0, "output": 55.0, "cached_in_input": True}
    default_haiku = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 2.0}
    example = _write(
        tmp_path / "pricing.example.json",
        {"gpt6-astra": default_astra, "haiku": default_haiku},
    )
    mine = {"input": 0, "output": 0, "cached_in_input": True}
    override = _write(tmp_path / "pricing.json", {"gpt6-astra": mine})
    monkeypatch.setattr(tracker, "PRICING_EXAMPLE_PATH", example)
    monkeypatch.setattr(tracker, "PRICING_PATH", override)
    table = tracker._load_pricing()
    assert table["gpt6-astra"] == _entry(mine)
    assert table["haiku"] == _entry(default_haiku)


def test_an_override_equal_to_an_older_default_still_wins(tmp_path, monkeypatch):
    # No stale-copy rule: whatever pricing.json says for a key is used.
    current = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 2.0}
    older = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25}
    example = _write(tmp_path / "pricing.example.json", {"haiku": current})
    override = _write(tmp_path / "pricing.json", {"haiku": older})
    monkeypatch.setattr(tracker, "PRICING_EXAMPLE_PATH", example)
    monkeypatch.setattr(tracker, "PRICING_PATH", override)
    assert tracker._load_pricing()["haiku"] == _entry(older)
