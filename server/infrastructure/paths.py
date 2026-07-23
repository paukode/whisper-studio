"""Single owner for on-disk app-data locations.

Modules were each deriving a DATA_DIR from their own ``__file__`` (three
``dirname()`` calls up to the repo root), which silently moves with whatever
checkout/worktree the server happens to run from — data written by a worktree
instance evaporates with the worktree. New code should resolve app data
through :func:`data_root` instead; existing per-module DATA_DIRs migrate here
over time.

Resolution order:
  1. ``WHISPER_DATA_DIR`` environment variable
  2. ``data_dir`` key in config.json
  3. ``<repo>/data`` (the historical default)
"""

import os


def repo_root() -> str:
    """The repository root of the RUNNING server's code tree."""
    # server/infrastructure/paths.py -> server/infrastructure -> server -> repo
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def data_root() -> str:
    """Where app data (result cache, config stores, DBs) lives. Env beats
    config beats the repo-relative default. Resolved on every call so tests
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
    return os.path.join(repo_root(), "data")
