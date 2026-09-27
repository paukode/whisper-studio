"""pricing.json holds genuine user overrides only.

Packaged installs used to seed ~/.whisper/pricing.json with a full copy of the
template. Since pricing.json wins per key, that copy froze every rate at the
install date and shadowed each later correction to pricing.example.json. The
loader now ignores an override entry that equals a rate some release shipped.
"""

import json
import subprocess

import pytest

from server.costs import tracker
from server.costs.pricing_history import SHIPPED_RATES
from server.infrastructure.paths import repo_root


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return str(path)


def _entry(raw):
    return tracker._coerce_pricing_entry(raw)


def test_the_current_template_is_recorded_as_shipped():
    # A rate change to pricing.example.json must append the new entry to the
    # history, or a later correction could not recognize today's copy.
    with open(tracker.PRICING_EXAMPLE_PATH) as f:
        raw = json.load(f)
    for key, val in raw.items():
        if key.startswith(("_", "$")):
            continue
        assert _entry(val) in SHIPPED_RATES.get(key, ()), f"{key} missing from SHIPPED_RATES"


def _entries(raw: dict) -> set[tuple[str, str]]:
    out = set()
    for key, val in raw.items():
        if key.startswith(("_", "$")):
            continue
        entry = _entry(val)
        if entry is not None:
            out.add((key, json.dumps(entry, sort_keys=True)))
    return out


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo_root(), capture_output=True, text=True)


def test_the_history_is_the_tagged_releases_plus_the_current_template():
    # Recognised: every rate a release could have seeded, so a stale copy is
    # always ignored. Not recognised: a value no release carried, which no
    # seeded copy can hold, so an override equal to it is a real decision.
    try:
        tags = _git("tag", "--list", "v*").stdout.split()
    except OSError:
        pytest.skip("git is not available")
    if not tags:
        pytest.skip("no release tags in this checkout (a shallow clone)")
    released = set()
    for tag in tags:
        shown = _git("show", f"{tag}:pricing.example.json")
        if shown.returncode == 0:  # releases before the file existed have none
            released |= _entries(json.loads(shown.stdout))
    with open(tracker.PRICING_EXAMPLE_PATH) as f:
        current = _entries(json.load(f))
    history = {
        (key, json.dumps(entry, sort_keys=True))
        for key, entries in SHIPPED_RATES.items()
        for entry in entries
    }
    assert released, "no tag carries pricing.example.json"
    assert history == released | current, (
        f"not released and not current: {sorted(history - released - current)}; "
        f"released but missing: {sorted((released | current) - history)}"
    )


def test_a_seeded_copy_of_an_old_template_does_not_shadow_the_current_rates(tmp_path, monkeypatch):
    # Every key's oldest shipped entry, i.e. a pricing.json seeded by an early
    # release and never touched.
    old_copy = {key: entries[0] for key, entries in SHIPPED_RATES.items()}
    monkeypatch.setattr(tracker, "PRICING_PATH", _write(tmp_path / "pricing.json", old_copy))
    table = tracker._load_pricing()
    monkeypatch.setattr(tracker, "PRICING_PATH", str(tmp_path / "none.json"))
    assert table == tracker._load_pricing()


def test_a_stale_entry_yields_to_a_corrected_default(tmp_path, monkeypatch):
    stale = SHIPPED_RATES["sonnet5"][0]
    corrected = SHIPPED_RATES["sonnet5"][-1]
    assert stale != corrected
    example = _write(tmp_path / "pricing.example.json", {"sonnet5": corrected})
    override = _write(tmp_path / "pricing.json", {"sonnet5": stale})
    monkeypatch.setattr(tracker, "PRICING_EXAMPLE_PATH", example)
    monkeypatch.setattr(tracker, "PRICING_PATH", override)
    assert tracker._load_pricing()["sonnet5"] == _entry(corrected)


def test_a_genuine_override_still_wins(tmp_path, monkeypatch):
    # For example GPT set to $0 while AWS bills it at $0.
    example = _write(
        tmp_path / "pricing.example.json",
        {"gpt6-astra": SHIPPED_RATES["gpt6-astra"][-1]},
    )
    mine = {"input": 0, "output": 0, "cached_in_input": True}
    override = _write(tmp_path / "pricing.json", {"gpt6-astra": mine})
    monkeypatch.setattr(tracker, "PRICING_EXAMPLE_PATH", example)
    monkeypatch.setattr(tracker, "PRICING_PATH", override)
    assert tracker._load_pricing()["gpt6-astra"] == _entry(mine)


def test_an_override_equal_to_a_rate_no_release_shipped_wins(tmp_path, monkeypatch):
    # GPT-6 Sol at OpenAI's direct rate, which the pricing note cites as the
    # base: an intermediate branch carried it, no release did.
    direct = {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}
    override = _write(tmp_path / "pricing.json", {"gpt6-sol": {**direct, "cached_in_input": True}})
    monkeypatch.setattr(tracker, "PRICING_PATH", override)
    assert tracker._load_pricing()["gpt6-sol"] == {**direct, "cached_in_input": True}
