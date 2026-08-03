"""WHISPER_HOME app-home layer (server/infrastructure/paths.py).

Two contracts under test:
  1. WHISPER_HOME unset → every helper resolves to the historical repo-root
     location (dev checkouts and the rest of the suite must be untouched).
  2. WHISPER_HOME set → config/models/storage/skills/plugins/logs/data all land
     under it, and bootstrap_home() seeds the user-editable files exactly once.

The path helpers read the env on every call, so they are exercised directly;
consumers that froze a module-level constant at import (CONFIG_PATH) are
exercised via importlib.reload with a restore in ``finally``.
"""

import importlib
import json
import os

from server.infrastructure import paths


def test_defaults_to_repo_root_without_whisper_home(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    root = paths.repo_root()
    assert paths.app_home() == root
    assert paths.config_dir() == root
    assert paths.models_root() == os.path.join(root, "models")
    assert paths.storage_root() == os.path.join(root, "storage")
    assert paths.skills_root() == os.path.join(root, "skills")
    assert paths.plugins_root() == os.path.join(root, "plugins")
    assert paths.logs_root() == os.path.join(root, "logs")


def test_whisper_home_moves_all_roots(monkeypatch, tmp_path):
    home = str(tmp_path / "apphome")
    monkeypatch.setenv("WHISPER_HOME", home)
    assert paths.app_home() == home
    assert paths.config_dir() == home
    assert paths.models_root() == os.path.join(home, "models")
    assert paths.storage_root() == os.path.join(home, "storage")
    assert paths.skills_root() == os.path.join(home, "skills")
    assert paths.plugins_root() == os.path.join(home, "plugins")
    assert paths.logs_root() == os.path.join(home, "logs")


def test_whisper_home_is_expanded_and_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WHISPER_HOME", "~/whisper-home")
    assert paths.app_home() == os.path.join(str(tmp_path), "whisper-home")


def test_data_root_defaults_under_whisper_home(monkeypatch, tmp_path):
    home = str(tmp_path / "apphome")
    monkeypatch.setenv("WHISPER_HOME", home)
    monkeypatch.delenv("WHISPER_DATA_DIR", raising=False)
    # Neutralize a data_dir the developer's live config.json may carry.
    import server.infrastructure.config as config

    monkeypatch.setattr(config, "get", lambda key, default=None: "")
    assert paths.data_root() == os.path.join(home, "data")


def test_data_dir_env_still_beats_whisper_home(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "elsewhere"))
    assert paths.data_root() == str(tmp_path / "elsewhere")


def test_bootstrap_home_noop_without_env(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    logs = os.path.join(paths.repo_root(), "logs")
    existed_before = os.path.isdir(logs)
    paths.bootstrap_home()
    # No tree is created at the repo root in dev mode.
    assert os.path.isdir(logs) == existed_before


def test_bootstrap_home_seeds_tree_and_files(monkeypatch, tmp_path):
    home = tmp_path / "apphome"
    monkeypatch.setenv("WHISPER_HOME", str(home))
    paths.bootstrap_home()

    for sub in ("models", "storage", "plugins", "logs", "data", "skills"):
        assert (home / sub).is_dir(), f"missing {sub}/"

    root = paths.repo_root()
    with open(os.path.join(root, "config.example.json")) as f:
        example_cfg = json.load(f)
    with open(home / "config.json") as f:
        seeded_cfg = json.load(f)
    assert seeded_cfg == example_cfg

    with open(os.path.join(root, "pricing.example.json")) as f:
        example_pricing = json.load(f)
    with open(home / "pricing.json") as f:
        seeded_pricing = json.load(f)
    assert seeded_pricing == example_pricing

    assert (home / "PROMPT_RULES.md").is_file()

    # skills/ is a copy of the repo tree.
    repo_skills = sorted(os.listdir(os.path.join(root, "skills")))
    assert sorted(os.listdir(home / "skills")) == repo_skills


def test_bootstrap_home_is_idempotent_and_never_overwrites(monkeypatch, tmp_path):
    home = tmp_path / "apphome"
    monkeypatch.setenv("WHISPER_HOME", str(home))
    paths.bootstrap_home()

    # User edits after the first boot must survive every later boot.
    (home / "config.json").write_text('{"mine": true}')
    (home / "PROMPT_RULES.md").write_text("no emoji")
    marker = home / "skills" / "my-own-skill.md"
    marker.write_text("---\nname: mine\n---\n")
    seeded = sorted(os.listdir(home / "skills"))

    paths.bootstrap_home()
    assert json.loads((home / "config.json").read_text()) == {"mine": True}
    assert (home / "PROMPT_RULES.md").read_text() == "no emoji"
    assert sorted(os.listdir(home / "skills")) == seeded

    # A skill the user deleted stays deleted (the tree is seeded once, whole).
    marker.unlink()
    paths.bootstrap_home()
    assert not marker.exists()


def test_config_path_constant_follows_whisper_home(monkeypatch, tmp_path):
    """CONFIG_PATH is frozen at import, so flipping the env needs a reload."""
    import server.infrastructure.config as config

    home = str(tmp_path / "apphome")
    monkeypatch.setenv("WHISPER_HOME", home)
    try:
        importlib.reload(config)
        assert config.CONFIG_PATH == os.path.join(home, "config.json")
        # The committed template stays with the code, not the app home.
        assert config.EXAMPLE_CONFIG_PATH == os.path.join(paths.repo_root(), "config.example.json")
    finally:
        monkeypatch.delenv("WHISPER_HOME", raising=False)
        importlib.reload(config)
    assert config.CONFIG_PATH == os.path.join(paths.repo_root(), "config.json")


def test_bootstrap_seeds_plugins_and_restores_protected(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path))
    from server.infrastructure import paths

    paths.bootstrap_home()
    seeded = tmp_path / "plugins" / "security_checks.py"
    assert seeded.is_file()
    # User edits survive re-bootstrap...
    seeded.write_text("# user-edited")
    paths.bootstrap_home()
    assert seeded.read_text() == "# user-edited"
    # ...but a deleted protected plugin is restored on the next boot.
    seeded.unlink()
    paths.bootstrap_home()
    assert seeded.is_file() and seeded.read_text() != "# user-edited"
