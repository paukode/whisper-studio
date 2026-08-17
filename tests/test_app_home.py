"""The two-root layout (server/infrastructure/paths.py).

Contracts under test:
  1. Dev checkout (WHISPER_HOME unset): every helper resolves to the historical
     repo-root location — the rest of the suite and every dev setup depend on it.
  2. Packaged install (WHISPER_HOME set): the app home keeps EXACTLY storage/
     and logs/; everything user-editable — the config layer, models, skills,
     plugins, agents, data — lives under ~/.whisper (user_root()).
     WHISPER_USER_DIR overrides user_root() (the conftest pins it per-test so
     no test can touch the real ~/.whisper).
  3. bootstrap_home() seeds the user-editable files exactly once, and MOVES a
     legacy Application Support tree into user_root() with nothing left
     behind: pre-data-era top-level stores are swept, stale duplicates are
     parked (never merged), compat symlinks from the first migration are
     removed, and migration 017 rewrites recorded absolute paths.
"""

import importlib
import json
import os

from server.infrastructure import paths


def test_defaults_to_repo_root_without_whisper_home(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    monkeypatch.delenv("WHISPER_USER_DIR", raising=False)
    root = paths.repo_root()
    assert paths.app_home() == root
    assert paths.user_root() == root
    assert paths.config_dir() == root
    assert paths.models_root() == os.path.join(root, "models")
    assert paths.storage_root() == os.path.join(root, "storage")
    assert paths.skills_root() == os.path.join(root, "skills")
    assert paths.plugins_root() == os.path.join(root, "plugins")
    assert paths.logs_root() == os.path.join(root, "logs")


def test_whisper_home_splits_app_and_user_roots(monkeypatch, tmp_path):
    home = str(tmp_path / "apphome")
    user = str(tmp_path / "userroot")
    monkeypatch.setenv("WHISPER_HOME", home)
    monkeypatch.setenv("WHISPER_USER_DIR", user)
    # App-owned: exactly the history and the logs.
    assert paths.app_home() == home
    assert paths.storage_root() == os.path.join(home, "storage")
    assert paths.logs_root() == os.path.join(home, "logs")
    # User-owned: everything hand-editable, config layer included.
    assert paths.user_root() == user
    assert paths.config_dir() == user
    assert paths.models_root() == os.path.join(user, "models")
    assert paths.skills_root() == os.path.join(user, "skills")
    assert paths.plugins_root() == os.path.join(user, "plugins")


def test_user_root_defaults_to_dot_whisper_when_packaged(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "apphome"))
    monkeypatch.delenv("WHISPER_USER_DIR", raising=False)
    assert paths.user_root() == os.path.join(str(tmp_path), ".whisper")


def test_whisper_home_is_expanded_and_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WHISPER_HOME", "~/whisper-home")
    assert paths.app_home() == os.path.join(str(tmp_path), "whisper-home")


def test_data_root_defaults_under_user_root(monkeypatch, tmp_path):
    user = str(tmp_path / "userroot")
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("WHISPER_USER_DIR", user)
    monkeypatch.delenv("WHISPER_DATA_DIR", raising=False)
    # Neutralize a data_dir the developer's live config.json may carry.
    import server.infrastructure.config as config

    monkeypatch.setattr(config, "get", lambda key, default=None: "")
    assert paths.data_root() == os.path.join(user, "data")


def test_data_dir_env_still_beats_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPER_HOME", str(tmp_path / "apphome"))
    monkeypatch.setenv("WHISPER_USER_DIR", str(tmp_path / "userroot"))
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "elsewhere"))
    assert paths.data_root() == str(tmp_path / "elsewhere")


def test_bootstrap_home_noop_without_env(monkeypatch):
    monkeypatch.delenv("WHISPER_HOME", raising=False)
    logs = os.path.join(paths.repo_root(), "logs")
    existed_before = os.path.isdir(logs)
    paths.bootstrap_home()
    # No tree is created at the repo root in dev mode.
    assert os.path.isdir(logs) == existed_before


def _packaged(monkeypatch, tmp_path):
    home = tmp_path / "apphome"
    user = tmp_path / "userroot"
    monkeypatch.setenv("WHISPER_HOME", str(home))
    monkeypatch.setenv("WHISPER_USER_DIR", str(user))
    monkeypatch.delenv("WHISPER_DATA_DIR", raising=False)
    import server.infrastructure.config as config

    monkeypatch.setattr(config, "get", lambda key, default=None: "")
    return home, user


def test_bootstrap_home_seeds_both_trees(monkeypatch, tmp_path):
    home, user = _packaged(monkeypatch, tmp_path)
    paths.bootstrap_home()

    for sub in ("storage", "logs"):
        assert (home / sub).is_dir(), f"missing app-side {sub}/"
    for sub in ("models", "plugins", "data", "skills"):
        assert (user / sub).is_dir(), f"missing user-side {sub}/"

    root = paths.repo_root()
    from server.infrastructure.config import FIRST_RUN_USER_CONFIG

    with open(user / "config.user.json") as f:
        assert json.load(f) == FIRST_RUN_USER_CONFIG
    assert not (user / "config.json").exists()
    assert not (home / "config.user.json").exists()

    with open(os.path.join(root, "pricing.example.json")) as f:
        example_pricing = json.load(f)
    with open(user / "pricing.json") as f:
        assert json.load(f) == example_pricing
    assert (user / "PROMPT_RULES.md").is_file()

    # skills/ is a copy of the repo tree, at the USER root.
    repo_skills = sorted(os.listdir(os.path.join(root, "skills")))
    assert sorted(os.listdir(user / "skills")) == repo_skills


def test_bootstrap_home_is_idempotent_and_never_overwrites(monkeypatch, tmp_path):
    home, user = _packaged(monkeypatch, tmp_path)
    paths.bootstrap_home()

    (user / "config.user.json").write_text('{"mine": true}')
    (user / "PROMPT_RULES.md").write_text("no emoji")
    marker = user / "skills" / "my-own-skill.md"
    marker.write_text("---\nname: mine\n---\n")
    seeded = sorted(os.listdir(user / "skills"))

    paths.bootstrap_home()
    assert json.loads((user / "config.user.json").read_text()) == {"mine": True}
    assert (user / "PROMPT_RULES.md").read_text() == "no emoji"
    assert sorted(os.listdir(user / "skills")) == seeded

    # A skill the user deleted stays deleted (the tree is seeded once, whole).
    marker.unlink()
    paths.bootstrap_home()
    assert not marker.exists()


def test_config_path_constant_follows_whisper_home(monkeypatch, tmp_path):
    """CONFIG_PATH is frozen at import, so flipping the env needs a reload."""
    import server.infrastructure.config as config

    home = str(tmp_path / "apphome")
    user = str(tmp_path / "userroot")
    monkeypatch.setenv("WHISPER_HOME", home)
    monkeypatch.setenv("WHISPER_USER_DIR", user)
    try:
        importlib.reload(config)
        assert config.CONFIG_PATH == os.path.join(user, "config.json")
        assert config.USER_CONFIG_PATH == os.path.join(user, "config.user.json")
        assert config.EXAMPLE_CONFIG_PATH == os.path.join(paths.repo_root(), "config.example.json")
    finally:
        monkeypatch.delenv("WHISPER_HOME", raising=False)
        monkeypatch.delenv("WHISPER_USER_DIR", raising=False)
        importlib.reload(config)
    assert config.CONFIG_PATH == os.path.join(paths.repo_root(), "config.json")
    assert config.USER_CONFIG_PATH == os.path.join(paths.repo_root(), "config.user.json")


def test_bootstrap_seeds_plugins_and_restores_protected(tmp_path, monkeypatch):
    home, user = _packaged(monkeypatch, tmp_path)
    paths.bootstrap_home()
    seeded = user / "plugins" / "security_checks.py"
    assert seeded.is_file()
    # User edits survive re-bootstrap...
    seeded.write_text("# user-edited")
    paths.bootstrap_home()
    assert seeded.read_text() == "# user-edited"
    # ...but a deleted protected plugin is restored on the next boot.
    seeded.unlink()
    paths.bootstrap_home()
    assert seeded.is_file() and seeded.read_text() != "# user-edited"


# ── the one-shot Application Support -> ~/.whisper relocation ────────────────


def test_bootstrap_relocates_a_pre_split_tree(monkeypatch, tmp_path):
    home, user = _packaged(monkeypatch, tmp_path)
    # A pre-split install: user data still lives under Application Support.
    for sub, marker in (
        ("models", "weights.gguf"),
        ("skills", "mine.md"),
        ("plugins", "mine.py"),
        ("data", "memory/fact.md"),
    ):
        p = home / sub / marker
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("precious")
    (home / "config.user.json").write_text('{"mine": true}')
    (home / "PROMPT_RULES.md").write_text("rules")
    agents = home / ".whisper" / "agents"
    agents.mkdir(parents=True)
    (agents / "pdf_extractor.md").write_text("agent")

    paths.bootstrap_home()

    for sub, marker in (
        ("models", "weights.gguf"),
        ("skills", "mine.md"),
        ("plugins", "mine.py"),
        ("data", "memory/fact.md"),
    ):
        moved = user / sub / marker
        assert moved.is_file() and moved.read_text() == "precious", f"{sub} not moved"
        # A clean cut-over: nothing is left behind, not even a symlink —
        # migration 017 rewrites the absolute paths recorded in sessions.db.
        assert not (home / sub).exists()
    assert json.loads((user / "config.user.json").read_text()) == {"mine": True}
    assert (user / "PROMPT_RULES.md").read_text() == "rules"
    assert (user / "agents" / "pdf_extractor.md").read_text() == "agent"
    assert not (home / ".whisper").exists()
    assert not (home / "config.user.json").exists()

    # Idempotent: a second boot leaves everything in place.
    paths.bootstrap_home()
    assert (user / "models" / "weights.gguf").read_text() == "precious"


def test_bootstrap_sweeps_pre_data_era_stores_and_stale_caches(monkeypatch, tmp_path):
    home, user = _packaged(monkeypatch, tmp_path)
    # Leftovers from before the data/ consolidation sat at the TOP level of
    # Application Support (memory, scratch, workspace_config.json, ...).
    (home / "global_memory").mkdir(parents=True)
    (home / "global_memory" / "fact.md").write_text("old fact")
    (home / "workspace_config.json").write_text('{"path": null}')
    (home / "pycache" / "3.13").mkdir(parents=True)
    (home / "pycache" / "3.13" / "x.pyc").write_text("bytecode")

    paths.bootstrap_home()

    assert (user / "data" / "global_memory" / "fact.md").read_text() == "old fact"
    assert (user / "data" / "workspace_config.json").is_file()
    assert not (home / "global_memory").exists()
    assert not (home / "workspace_config.json").exists()
    # The bytecode cache is machine noise: deleted, not moved.
    assert not (home / "pycache").exists()


def test_bootstrap_parks_stale_copies_instead_of_clobbering(monkeypatch, tmp_path):
    home, user = _packaged(monkeypatch, tmp_path)
    # The live store already moved; a stale duplicate lingers at the old spot.
    (user / "data" / "global_memory").mkdir(parents=True)
    (user / "data" / "global_memory" / "fact.md").write_text("live")
    (home / "global_memory").mkdir(parents=True)
    (home / "global_memory" / "fact.md").write_text("stale")

    paths.bootstrap_home()

    # The live copy is untouched; the stale one is parked, not merged or lost.
    assert (user / "data" / "global_memory" / "fact.md").read_text() == "live"
    parked = user / "data" / "legacy_app_support" / "global_memory" / "fact.md"
    assert parked.read_text() == "stale"
    assert not (home / "global_memory").exists()


def test_bootstrap_removes_compat_symlinks_from_the_first_migration(monkeypatch, tmp_path):
    home, user = _packaged(monkeypatch, tmp_path)
    # The first version of the migration left old -> new symlinks behind.
    (user / "models").mkdir(parents=True)
    home.mkdir(parents=True, exist_ok=True)
    os.symlink(str(user / "models"), str(home / "models"))

    paths.bootstrap_home()

    assert not (home / "models").exists()
    assert (user / "models").is_dir()


def test_bootstrap_never_clobbers_an_existing_new_tree(monkeypatch, tmp_path):
    home, user = _packaged(monkeypatch, tmp_path)
    (home / "models").mkdir(parents=True)
    (home / "models" / "old.gguf").write_text("old")
    (user / "models").mkdir(parents=True)
    (user / "models" / "new.gguf").write_text("new")

    paths.bootstrap_home()

    # Both-exist: the new location wins; the old copy is parked under the data
    # root — never merged, never deleted, and Application Support ends clean.
    assert (user / "models" / "new.gguf").read_text() == "new"
    assert not (user / "models" / "old.gguf").exists()
    parked = user / "data" / "legacy_app_support" / "models" / "old.gguf"
    assert parked.read_text() == "old"
    assert not (home / "models").exists()


def test_bootstrap_leaves_overridden_data_dir_alone(monkeypatch, tmp_path):
    home, user = _packaged(monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("WHISPER_DATA_DIR", str(elsewhere))
    (home / "data" / "memory").mkdir(parents=True)

    paths.bootstrap_home()

    # The user already relocated data; it is theirs to manage. The old data
    # tree stays where it is, and the override location is simply created.
    assert (home / "data" / "memory").is_dir()
    assert not (user / "data" / "memory").exists()
    assert elsewhere.is_dir()


def test_migration_017_rewrites_recorded_paths(monkeypatch, tmp_path):
    import sqlite3

    home, user = _packaged(monkeypatch, tmp_path)
    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE attachments (id TEXT, source_path TEXT)")
    conn.execute("CREATE TABLE tasks (id TEXT, output_path TEXT)")
    old_prefix = os.path.join(str(home), "data")
    conn.execute(
        "INSERT INTO attachments VALUES ('a1', ?)",
        (os.path.join(old_prefix, "attachments", "doc.docx"),),
    )
    conn.execute(
        "INSERT INTO tasks VALUES ('t1', ?)",
        (os.path.join(old_prefix, "background_output", "t1.log"),),
    )
    conn.execute("INSERT INTO tasks VALUES ('t2', '/somewhere/else.log')")
    conn.commit()

    _m = importlib.import_module("server.migrations.017_rewrite_data_paths")

    _m.migrate(conn)
    rows = dict(conn.execute("SELECT id, source_path FROM attachments"))
    assert rows["a1"] == os.path.join(str(user), "data", "attachments", "doc.docx")
    tasks = dict(conn.execute("SELECT id, output_path FROM tasks"))
    assert tasks["t1"] == os.path.join(str(user), "data", "background_output", "t1.log")
    # Unrelated paths are untouched, and re-running is a no-op.
    assert tasks["t2"] == "/somewhere/else.log"
    _m.migrate(conn)
    assert dict(conn.execute("SELECT id, source_path FROM attachments"))["a1"] == rows["a1"]
