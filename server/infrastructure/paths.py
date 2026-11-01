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
  root. Holds exactly two things: storage/ (sessions.db and the workspace
  indexes — the historical record a hand-edit must never destroy) and logs/.
- User root (:func:`user_root`): ``~/.whisper`` in a packaged install —
  everything a person browses, edits, or deletes by hand: models/, skills/,
  plugins/, agents/, the config layer (config.user.json, pricing.json,
  WHISPER.md, rules/), and data/ (memory, global_memory, session_memory,
  workflows, attachments, …) — one visible, predictable place, the way
  ~/.claude works for Claude Code. In a dev checkout (WHISPER_HOME unset)
  it is the repo root, so dev layout is unchanged. ``WHISPER_USER_DIR``
  overrides it for tests and relocation.

data_root() resolution order:
  1. ``WHISPER_DATA_DIR`` environment variable
  2. ``data_dir`` key in config.user.json
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
    """Directory holding config.user.json / pricing.json, user-editable files, so
    they live with the rest of the user data."""
    return user_root()


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
    except Exception as e:  # noqa: BLE001 — config may be mid-import at boot
        # Swallowing silently once hid a circular-import bug for weeks (a
        # configured data_dir was ignored); keep booting, but say so.
        log.warning("data_root: config unavailable (%s); using default data dir", e)
        configured = ""
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(user_root(), "data")


def app_storage_roots() -> list[str]:
    """The folders the app owns on the user's behalf, where its own writes
    belong and need no approval (server/security/permissions.py): the user
    root (``~/.whisper``: rules, the profile, skills, agents, models) and the
    data root (scratch, memory, workflows). In a dev checkout the user root IS
    the repo root, which is also the most likely connected workspace, so there
    only its app-owned children count, never the whole tree."""
    root = user_root()
    data = data_root()
    if root != repo_root():
        return [root, data]
    return [
        data,
        os.path.join(root, "rules"),
        os.path.join(root, "WHISPER.md"),
        models_root(),
        skills_root(),
        plugins_root(),
        os.path.join(root, "agents"),
    ]


# The app's own grant and config files. Each one decides what the assistant
# may do next (the permission rules, the MCP servers, the hooks and their
# trust, the trusted skills, the folder grants, the connected workspace, the
# enabled plugins, the scheduled jobs, the trusted preview commands), so the
# app storage rule above does not cover them: a tool write to one follows the
# permission mode instead (server/security/permissions.py, the app-config
# table), never the no-card allow the rest of the app's folder gets, and a
# command that no person reviewed cannot write one through the OS sandbox
# (server/sandbox.py). These live in the data root.
APP_CONFIG_FILES = (
    "permissions.json",
    "mcp_servers.json",
    "hooks.json",
    "hooks_trust.json",
    "skills_config.json",
    "folder_grants.json",
    "workspace_config.json",
    "plugins_config.json",
    "cron_jobs.json",
    "launch_trust.json",
)

# The user config layer (config.user.json): the ``data_dir`` key moves the whole
# data root and ``permission_mode`` sets the default mode, so it carries the same
# power. The old config.json is no longer read, but it is still the user's own
# file (it can hold API keys), so it stays protected too. Both live in the user
# root.
_USER_ROOT_CONFIG_FILES = (
    "config.user.json",
    "config.json",
)


def app_config_paths() -> list[str]:
    """The absolute paths of the app's own grant and config files, across the
    data root (APP_CONFIG_FILES) and the user root (_USER_ROOT_CONFIG_FILES)."""
    data = data_root()
    user = user_root()
    paths = [os.path.join(data, name) for name in APP_CONFIG_FILES]
    paths += [os.path.join(user, name) for name in _USER_ROOT_CONFIG_FILES]
    return paths


def app_config_dirs() -> list[str]:
    """Directories whose contents are app code or agent types the assistant
    must not rewrite without a person: the installed plugin modules (they load
    into the server process with no sandbox) and the user's custom agent types
    (each one can grant a running agent its own tools)."""
    return [plugins_root(), os.path.join(user_root(), "agents")]


def _seed_file(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` unless ``dst`` already exists. Best-effort: a
    missing template or an unwritable target logs instead of crashing boot."""
    if os.path.exists(dst) or not os.path.exists(src):
        return
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        log.info("Seeded %s from %s", dst, src)
    except OSError as e:
        log.warning("Could not seed %s from %s: %s", dst, src, e)


def bootstrap_home() -> None:
    """First-run setup for a packaged install: create the WHISPER_HOME and
    ~/.whisper trees and seed the user-editable files from the read-only code
    dir. No-op when WHISPER_HOME is unset (dev checkout: everything already
    lives at the repo root). Idempotent (existing files and directories are
    never overwritten) and safe: any single failure logs and moves on rather
    than blocking boot."""
    if not os.environ.get("WHISPER_HOME", "").strip():
        return
    home = app_home()
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
    # deltas live in config.user.json. seed_user_config() writes the first-run
    # defaults into a new config.user.json on a fresh install. It never copies
    # the example (a full copy shadowed shipped model updates) and never reads
    # or changes an old config.json.
    try:
        from server.infrastructure.config import seed_user_config

        seed_user_config()
    except Exception as e:  # never block boot on the first-run seed
        log.warning("First-run config seed skipped: %s", e)
    # pricing.json is NOT seeded: it holds only the user's own per-key rate
    # overrides over the shipped pricing.example.json, and a full copy of the
    # template froze every rate at install time. The shipped global rules file
    # is copied from repo_root() (read-only in a bundle) when missing; after
    # that the user owns rules/ entirely (server/prompts/rules.py).
    _seed_file(
        os.path.join(repo_root(), "rules", "global", "rules.md"),
        os.path.join(user_root(), "rules", "global", "rules.md"),
    )
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
