"""Where a workspace's index lives on disk.

Index data lives under the app's ``storage/`` dir (alongside sessions.db), one
subdirectory per workspace named ``<folder-name>-<hash>`` so you can tell at a
glance which is which (e.g. ``04_Career-98309860``). The hash suffix (first 8
of a sha1 of the absolute path) keeps two folders with the same basename apart.
Each workspace dir holds:

    index.db — SQLite: files manifest, chunks (text + 1024-d vector BLOB),
               graph nodes/edges, and meta (workspace, last_indexed_at, …).

(There is no separate vectors file — vectors are stored as BLOBs inside index.db.)
"""

import hashlib
import os
import re

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX_DATA_DIR = os.path.join(SCRIPT_DIR, "storage", "index")


def _hash(ws_path: str) -> str:
    real = os.path.normpath(os.path.abspath(os.path.expanduser(ws_path)))
    return hashlib.sha1(real.encode("utf-8")).hexdigest()[:8]


def _slug(ws_path: str) -> str:
    """Human-readable, filesystem-safe label from the workspace's folder name."""
    base = os.path.basename(os.path.normpath(os.path.abspath(os.path.expanduser(ws_path))))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-.")
    return slug[:48] or "workspace"


def workspace_index_dir(ws_path: str) -> str:
    return os.path.join(INDEX_DATA_DIR, f"{_slug(ws_path)}-{_hash(ws_path)}")


def _db_filename(embed_backend: str | None) -> str:
    """The index DB filename for an embed backend. Vectors from different
    embedders aren't comparable, so each backend gets its OWN db file inside the
    workspace dir. qwen3 (and the legacy default) keep ``index.db`` so existing
    indexes are never orphaned; other backends get a sibling ``index-<b>.db``."""
    if embed_backend in (None, "", "qwen3"):
        return "index.db"
    safe = re.sub(r"[^a-z0-9]+", "", embed_backend.lower()) or "x"
    return f"index-{safe}.db"


def _active_embed_backend() -> str:
    """The embed backend selected by the current model mode (defaults to qwen3
    if the resolver is unavailable, e.g. very early import)."""
    try:
        from server.infrastructure.model_mode import resolve_backend

        return resolve_backend("embed")
    except Exception:
        return "qwen3"


def db_path(ws_path: str, embed_backend: str | None = None) -> str:
    """Path to a workspace's index DB. With no explicit backend it routes to the
    active embed backend's DB, so reads/writes always hit the index built with
    the embedder that's in effect."""
    if embed_backend is None:
        embed_backend = _active_embed_backend()
    return os.path.join(workspace_index_dir(ws_path), _db_filename(embed_backend))


def is_indexed(ws_path: str, embed_backend: str | None = None) -> bool:
    """Whether the folder has an index for the given (or active) embed backend."""
    return os.path.exists(db_path(ws_path, embed_backend))
