"""Single owner for on-disk app-data locations.

Modules were each deriving a DATA_DIR from their own ``__file__`` (three
``dirname()`` calls up to the repo root), which silently moves with whatever
checkout/worktree the server happens to run from — data written by a worktree
instance evaporates with the worktree. New code should resolve app data
through :func:`data_root` instead; existing per-module DATA_DIRs migrate here
over time.

Two roots, split by who edits what:

- App home (:func:`app_home`): ``WHISPER_HOME`` when set (a packaged .app
  pointing at ~/Library/Application Support/WhisperStudio), else the repo
  root. Holds what the APP owns: storage/ (sessions.db and the workspace
  indexes — the historical record), logs/, and the config layer
  (config.user.json, pricing.json, PROMPT_RULES.md).
- User root (:func:`user_root`): ``~/.whisper`` in a packaged install, so the
  things a person browses, edits, or deletes by hand — models/, skills/,
  plugins/, and data/ (memory, global_memory, session_memory, workflows,
  attachments, …) — sit in one visible, predictable place, the way
  ~/.claude does for Claude Code. In a dev checkout (WHISPER_HOME unset)
  it is the repo root, so dev layout is unchanged. ``WHISPER_USER_DIR``
  overrides it for tests and relocation.

On the first packaged boot after this split, :func:`bootstrap_home` MOVES the
existing Application Support copies of those four directories into ~/.whisper
(same-volume rename, instant) and leaves a symlink at each old location, so
absolute paths recorded in sessions.db keep resolving and an older build that
runs afterwards finds the same store instead of reseeding a second one.

data_root() resolution order:
  1. ``WHISPER_DATA_DIR`` environment variable
  2. ``data_dir`` key in config.json
  3. ``<user_root>/data`` (== ``<repo>/data`` in a dev checkout)
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


def user_root() -> str:
    """Where user-EDITABLE state lives: ``~/.whisper`` in a packaged install
    (WHISPER_HOME set), the repo root in a dev checkout. ``WHISPER_USER_DIR``
    overrides both. Resolved on every call, like app_home()."""
    env = os.environ.get("WHISPER_USER_DIR", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if os.environ.get("WHISPER_HOME", "").strip():
        return os.path.expanduser("~/.whisper")
    return repo_root()


def models_root() -> str:
    """Downloaded model weights (GGUF, mlx-whisper, GLiNER, embedders, …)."""
    return os.path.join(user_root(), "models")


def storage_root() -> str:
    """SQLite stores (sessions.db and friends) and the per-workspace indexes.
    Deliberately under app_home(), not user_root(): the session history is the
    one thing a hand-edit or rm in ~/.whisper must never be able to destroy."""
    return os.path.join(app_home(), "storage")


def skills_root() -> str:
    """User-editable skill definitions (markdown + folder skills)."""
    return os.path.join(user_root(), "skills")


def plugins_root() -> str:
    """User-installed plugin modules."""
    return os.path.join(user_root(), "plugins")


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
    return os.path.join(user_root(), "data")


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


def _relocate_legacy_dir(old: str, new: str) -> None:
    """One-shot move of a user-data directory out of Application Support into
    ~/.whisper. Same-volume rename (instant, atomic, any size); a symlink stays
    behind at the old location so absolute paths recorded in sessions.db keep
    resolving and an older build reuses the moved store instead of reseeding a
    second one. If the rename cannot work (cross-volume), the fallback symlink
    points new -> old instead — never a multi-gigabyte copy during boot, which
    would outlive the app shell's health deadline. Idempotent: a prior run
    leaves a symlink at ``old``, which is skipped."""
    if os.path.islink(old) or not os.path.isdir(old):
        return
    if os.path.exists(new):
        log.warning("Not relocating %s: %s already exists; using the new location", old, new)
        return
    try:
        os.makedirs(os.path.dirname(new), exist_ok=True)
        os.rename(old, new)
        os.symlink(new, old)
        log.info("Relocated %s -> %s (symlink left behind)", old, new)
    except OSError as e:
        log.warning("Could not relocate %s -> %s (%s); linking in place", old, new, e)
        try:
            if not os.path.exists(new):
                os.symlink(old, new)
        except OSError as e2:
            log.warning("Could not link %s -> %s: %s", new, old, e2)


def bootstrap_home() -> None:
    """First-run setup for a packaged install: create the WHISPER_HOME and
    ~/.whisper trees, move any pre-split user data out of Application Support,
    and seed the user-editable files from the read-only code dir. No-op when
    WHISPER_HOME is unset (dev checkout: everything already lives at the repo
    root). Idempotent — existing files and directories are never overwritten —
    and safe: any single failure logs and moves on rather than blocking boot."""
    if not os.environ.get("WHISPER_HOME", "").strip():
        return
    home = app_home()
    # Move first, then create: a fresh empty dir at the new location would
    # defeat the move-only-when-destination-missing rule forever.
    _relocate_legacy_dir(os.path.join(home, "models"), models_root())
    _relocate_legacy_dir(os.path.join(home, "skills"), skills_root())
    _relocate_legacy_dir(os.path.join(home, "plugins"), plugins_root())
    # data/ moves only while it actually resolves to <user_root>/data — a
    # WHISPER_DATA_DIR or config data_dir override means the user already
    # relocated it themselves, and it is theirs to manage.
    if data_root() == os.path.join(user_root(), "data"):
        _relocate_legacy_dir(os.path.join(home, "data"), data_root())
    for d in (
        home,
        user_root(),
        models_root(),
        storage_root(),
        plugins_root(),
        logs_root(),
        data_root(),
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
