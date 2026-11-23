"""Keep the on-device chat models in ``config.user.json`` and on disk as one set.

The contract (user decision 2026-11-23):

  * a local chat model has a ``chat_models`` entry if and only if its weights
    are on disk under ``models_root()``;
  * the only exception is a download in flight (running or queued): its entry
    exists before the file lands, so the row can show progress;
  * deleting a local model deletes both, from either Settings tab.

``reconcile()`` repairs any drift in both directions:

  * weights with no entry (an entry lost from the config, a file copied in) are
    ADOPTED: the entry is written again, so Models and Discover agree. Only
    weights the app can identify are adopted: a Discover recommendation's
    folder, or a folder named the way a Discover install names it
    (``owner__repo`` for GGUF, ``owner__repo__mlx`` for MLX). Anything else in
    the folder stays untouched;
  * an entry with no weights and no download (the file deleted in Finder, a
    failed or cancelled download, a hand-typed entry) is REMOVED, with its
    leftover partial files.

Every read that lists local models (the Models catalog, Discover's
Recommended and search) runs it first, and so does app start. ``LOCK`` also
covers the install path, so a reconcile can never land between an install's
config write and its download start and drop the new entry.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from collections import Counter
from contextlib import contextmanager

from server.infrastructure import config as config_mod
from server.infrastructure.paths import models_root
from server.model_browser import compat, gguf_kv, install

log = logging.getLogger("whisper-studio")

LOCK = threading.RLock()

_MLX_DIR_SUFFIX = "__mlx"

# Keys whose weights are being fetched in this process outside the download
# manager (runtime.ensure_downloaded called by a chat load or the workspace
# dialog). The manager's own jobs are tracked by the manager.
_fetching: Counter[str] = Counter()
_fetching_lock = threading.Lock()


@contextmanager
def fetching(key: str):
    """Mark ``key`` as downloading for the duration of the block."""
    with _fetching_lock:
        _fetching[key] += 1
    try:
        yield
    finally:
        with _fetching_lock:
            _fetching[key] -= 1
            if _fetching[key] <= 0:
                del _fetching[key]


# What reconcile changed and the UI has not shown yet. Any read path may run
# the reconcile that removes a failed download's entry; the Models catalog
# hands these to the UI once, so the reason is never lost to a silent caller.
_notices: list[dict] = []


def take_notices() -> list[dict]:
    """Every pending change notice, oldest first, and clear them."""
    with LOCK:
        out = list(_notices)
        _notices.clear()
    return out


def _is_fetching(key: str) -> bool:
    with _fetching_lock:
        return _fetching.get(key, 0) > 0


def _repo_from_dir(dir_name: str) -> str | None:
    """The repo id a Discover install named this folder after, or None when the
    name does not round-trip through ``install._dir_name``."""
    if "__" not in dir_name:
        return None
    owner, _, name = dir_name.partition("__")
    repo = f"{owner}/{name}"
    if not owner or not name or install._dir_name(repo) != dir_name:
        return None
    return repo


def _header_ctx(path: str) -> int | None:
    """The context length a local GGUF header declares, or None if unreadable."""
    try:
        with open(path, "rb") as f:
            meta = gguf_kv.read_metadata(f)
    except Exception:  # best effort: a bad header only costs the 32K default
        return None
    arch = meta.get("general.architecture")
    value = meta.get(f"{arch}.context_length") if arch else None
    return int(value) if isinstance(value, int) and value > 0 else None


def _gguf_files(root: str) -> list[str]:
    """Complete chat GGUF files under ``root`` (paths relative to it), first
    shard only and only when every shard is present. hf keeps partials under
    ``.cache``, so a file outside it is a finished download."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if not name.lower().endswith(".gguf"):
                continue
            if compat.is_mmproj(rel) or compat.is_mtp_head(rel):
                continue
            info = compat.shard_info(name)
            if info is not None:
                if info[1] != 1:
                    continue
                sub = os.path.dirname(rel)
                shards = compat.shard_filenames(name)
                if not all(os.path.exists(os.path.join(root, sub, s)) for s in shards):
                    continue
            out.append(rel)
    return sorted(out)


def _claimed() -> tuple[set[str], set[tuple[str, str]]]:
    """(whole folders owned by an entry, (folder, file) pairs owned by a GGUF
    entry). Static catalog entries (speech, index) own their top folder."""
    from server.models_manager import catalog

    whole: set[str] = set()
    files: set[tuple[str, str]] = set()
    for e in catalog.entries():
        top = e.dir_name.split(os.sep, 1)[0]
        if e.group != catalog.GROUP_LOCAL_CHAT:
            whole.add(top)
        elif e.gguf_filename:
            files.add((e.dir_name, e.gguf_filename))
        else:
            whole.add(e.dir_name)
    return whole, files


def _orphans() -> list[tuple[str, dict]]:
    """(key, entry) for every identifiable weight on disk that no config entry
    owns. Keys already in the config are never overwritten."""
    from server.infrastructure.config import unfiltered_chat_models
    from server.local.registry import RECOMMENDED_LOCAL_MODELS
    from server.local.runtime import MLX_COMPLETE_MARKER

    root = models_root()
    if not os.path.isdir(root):
        return []
    _ids, meta = unfiltered_chat_models()
    taken = set(meta)
    whole, files = _claimed()
    rec_by_dir = {r["dir"]: (k, r) for k, r in RECOMMENDED_LOCAL_MODELS.items()}

    found: list[tuple[str, dict]] = []
    for dir_name in sorted(os.listdir(root)):
        path = os.path.join(root, dir_name)
        if dir_name.startswith(".") or not os.path.isdir(path) or dir_name in whole:
            continue

        if dir_name.endswith(_MLX_DIR_SUFFIX):
            repo = _repo_from_dir(dir_name[: -len(_MLX_DIR_SUFFIX)])
            if repo and os.path.exists(os.path.join(path, MLX_COMPLETE_MARKER)):
                key, entry = install.build_mlx_entry(repo, None, None)
                if key not in taken and entry["dir"] == dir_name:
                    found.append((key, entry))
                    taken.add(key)
            continue

        rec = rec_by_dir.get(dir_name)
        repo = rec[1]["repo_id"] if rec else _repo_from_dir(dir_name)
        if repo is None:
            continue
        for filename in _gguf_files(path):
            if (dir_name, filename) in files:
                continue
            if rec and filename == rec[1]["filename"]:
                key, entry = install.build_recommended_entry(rec[0])
            else:
                quant = compat.parse_quant(os.path.basename(filename))
                if quant is None:
                    continue
                key, entry = install.build_entry(
                    repo_id=repo,
                    filename=filename,
                    quant=quant,
                    label=None,
                    n_ctx=None,
                    header_ctx=_header_ctx(os.path.join(path, filename)),
                )
                entry["dir"] = dir_name
            if key in taken:
                continue
            found.append((key, entry))
            taken.add(key)
    return found


def _stale() -> list[tuple[object, str]]:
    """(catalog entry, reason) for every USER-layer local entry with no weights
    and no download running or queued."""
    from server.models_manager import catalog, manager

    raw = config_mod._load_user_config().get("chat_models")
    user_keys = set(raw) if isinstance(raw, dict) else set()
    out: list[tuple[object, str]] = []
    for e in catalog.entries():
        if e.group != catalog.GROUP_LOCAL_CHAT or e.key not in user_keys:
            continue
        if _is_fetching(e.key):
            continue
        state, error = manager.state_of(e)
        if state in ("installed", "downloading", "queued"):
            continue
        out.append((e, error or "Its file is not on disk."))
    return out


def _prune_empty(root: str) -> None:
    """Remove ``root`` and its folders bottom-up while they hold no files."""
    if not os.path.isdir(root):
        return
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass  # not empty


def _drop_leftovers(e) -> None:
    """Delete what a stale entry left behind, and nothing else: a GGUF entry's
    own shards, partials and hf bookkeeping; an MLX entry's ``__mlx`` snapshot
    folder when no other entry uses it. A folder shared with other files (other
    quants, other models) keeps them."""
    from server.models_manager import catalog

    root = catalog.model_dir(e)
    if not os.path.isdir(root):
        return
    if e.gguf_filename:
        for path in catalog.gguf_paths(e):
            os.remove(path)
    elif e.dir_name.endswith(_MLX_DIR_SUFFIX) and not any(
        o.key != e.key and o.dir_name == e.dir_name for o in catalog.entries()
    ):
        shutil.rmtree(root)
    _prune_empty(root)


def reconcile() -> dict:
    """Make config and disk match. Returns what changed:
    ``{"adopted": [{key, label}], "removed": [{key, label, reason}]}``."""
    from server.models_manager import manager

    adopted: list[dict] = []
    removed: list[dict] = []
    with LOCK:
        manager.reap()
        for key, entry in _orphans():
            install.write_entry(key, entry)
            adopted.append({"key": key, "label": entry["label"]})
            _notices.append({"kind": "adopted", "key": key, "label": entry["label"]})
            log.info("Model sync: adopted %s from %s on disk.", key, entry["dir"])
        for e, reason in _stale():
            try:
                _drop_leftovers(e)
            except OSError as exc:
                log.warning("Model sync: kept %s, its files could not go: %s", e.key, exc)
                continue
            install.remove_entry(e.key)
            manager.forget(e.key)
            removed.append({"key": e.key, "label": e.label, "reason": reason})
            _notices.append({"kind": "removed", "key": e.key, "label": e.label, "reason": reason})
            log.info("Model sync: removed %s (%s).", e.key, reason)
    return {"adopted": adopted, "removed": removed}


def reconcile_quietly() -> dict:
    """``reconcile()`` for read paths: a sync problem is logged, never allowed
    to break the listing that triggered it."""
    try:
        return reconcile()
    except Exception:
        log.exception("Model sync failed.")
        return {"adopted": [], "removed": []}
