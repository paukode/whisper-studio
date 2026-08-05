"""Single owner for on-disk app-data locations.

Modules were each deriving a DATA_DIR from their own ``__file__`` (three
``dirname()`` calls up to the repo root), which silently moves with whatever
checkout/worktree the server happens to run from — data written by a worktree
instance evaporates with the worktree. New code should resolve app data
through :func:`data_root` instead; existing per-module DATA_DIRs migrate here
over time.

App home (:func:`app_home`): when the ``WHISPER_HOME`` environment variable is
set (a packaged .app pointing at, e.g., ~/Library/Application Support/Whisper
Studio), ALL user-mutable state — config.json, pricing.json, PROMPT_RULES.md,
models/, storage/, data/, skills/, plugins/, logs/ — lives under that
directory, and the repo/bundle stays read-only code. When it is NOT set, every
helper resolves to the historical repo-root-relative location, so a dev
checkout behaves exactly as before.

data_root() resolution order:
  1. ``WHISPER_DATA_DIR`` environment variable
  2. ``data_dir`` key in config.json
  3. ``<app_home>/data`` (== ``<repo>/data``, the historical default, when
     WHISPER_HOME is unset)
"""

import logging
import os
import shutil

log = logging.getLogger("whisper-studio")


def repo_root() -> str:
    """The repository root of the RUNNING server's code tree."""
    # server/infrastructure/paths.py -> server/infrastructure -> server -> repo
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def app_home() -> str:
    """Where user-mutable state lives: ``WHISPER_HOME`` when set (packaged
    install), else the repo root (dev checkout — historical behavior).
    Resolved on every call so tests can flip the env without a restart."""
    env = os.environ.get("WHISPER_HOME", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return repo_root()


def models_root() -> str:
    """Downloaded model weights (GGUF, mlx-whisper, GLiNER, embedders, …)."""
    return os.path.join(app_home(), "models")


def storage_root() -> str:
    """SQLite stores (sessions.db and friends) and the per-workspace indexes."""
    return os.path.join(app_home(), "storage")


def skills_root() -> str:
    """User-editable skill definitions (markdown + folder skills)."""
    return os.path.join(app_home(), "skills")


def plugins_root() -> str:
    """User-installed plugin modules."""
    return os.path.join(app_home(), "plugins")


def logs_root() -> str:
    """Log files that must survive outside the read-only bundle."""
    return os.path.join(app_home(), "logs")


def config_dir() -> str:
    """Directory holding config.json / pricing.json / PROMPT_RULES.md."""
    return app_home()


def data_root() -> str:
    """Where app data (result cache, config stores, DBs) lives. Env beats
    config beats the app-home default. Resolved on every call so tests
    and long-lived processes see env/config changes without a restart."""
    env = os.environ.get("WHISPER_DATA_DIR", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    try:
        # Lazy import: config pulls fastapi; keep paths importable at early boot.
        from server.infrastructure import config as _config

        configured = str(_config.get("data_dir", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(app_home(), "data")


def _seed_file(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` unless ``dst`` already exists. Best-effort: a
    missing template or an unwritable target logs instead of crashing boot."""
    if os.path.exists(dst) or not os.path.exists(src):
        return
    try:
        shutil.copyfile(src, dst)
        log.info("Seeded %s from %s", dst, src)
    except OSError as e:
        log.warning("Could not seed %s from %s: %s", dst, src, e)


def bootstrap_home() -> None:
    """First-run setup for a packaged install: create the WHISPER_HOME tree and
    seed the user-editable files from the read-only code dir. No-op when
    WHISPER_HOME is unset (dev checkout: everything already lives at the repo
    root). Idempotent — existing files and directories are never overwritten —
    and safe: any single failure logs and moves on rather than blocking boot."""
    if not os.environ.get("WHISPER_HOME", "").strip():
        return
    home = app_home()
    for d in (
        home,
        models_root(),
        storage_root(),
        plugins_root(),
        logs_root(),
        os.path.join(home, "data"),
    ):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            log.warning("Could not create %s: %s", d, e)
    # The user's config is a two-layer split: the SYSTEM catalog + defaults ship
    # in the read-only config.example.json (repo_root()), and only the user's
    # deltas live in config.user.json. migrate_user_config() creates an empty
    # config.user.json on a fresh install and, on an upgrade, splits any legacy
    # monolithic config.json into config.user.json (backing it up first). We do
    # NOT seed config.json from the example any more — that copy is exactly what
    # shadowed shipped model/pricing updates after first run.
    try:
        from server.infrastructure.config import migrate_user_config

        migrate_user_config()
    except Exception as e:  # never block boot on the config split
        log.warning("Config migration/seed skipped: %s", e)
    # pricing.json still starts as a copy of the committed template (pricing stays
    # SYSTEM-seeded — there is no user pricing layer); PROMPT_RULES.md too. Both
    # live at repo_root() (read-only in a bundle) and are only copied when missing.
    _seed_file(
        os.path.join(repo_root(), "pricing.example.json"), os.path.join(home, "pricing.json")
    )
    _seed_file(os.path.join(repo_root(), "PROMPT_RULES.md"), os.path.join(home, "PROMPT_RULES.md"))
    # Seed the whole skills/ tree once; after that the user owns it (imports,
    # edits, deletions must not be resurrected on the next boot).
    skills_src = os.path.join(repo_root(), "skills")
    if not os.path.isdir(skills_root()):
        try:
            if os.path.isdir(skills_src):
                shutil.copytree(skills_src, skills_root())
                log.info("Seeded skills into %s", skills_root())
            else:
                os.makedirs(skills_root(), exist_ok=True)
        except OSError as e:
            log.warning("Could not seed skills into %s: %s", skills_root(), e)
    # Plugins are seeded per-file, every boot: user edits survive (existing
    # files are never overwritten), but a deleted file is restored — the
    # PROTECTED security_checks plugin is part of the safety contract and
    # must not be lost when the code dir is a read-only bundle.
    plugins_src = os.path.join(repo_root(), "plugins")
    if os.path.isdir(plugins_src):
        for entry in os.listdir(plugins_src):
            if entry.endswith((".py", ".md")):
                _seed_file(os.path.join(plugins_src, entry), os.path.join(plugins_root(), entry))
