"""
GitFileWatcher — polls .git/HEAD, .git/config, and the current branch's
ref file once per second, fires subscribers when any of them mutate.

Direct port of Claude Code's `GitFileWatcher` (utils/git/gitFilesystem.ts).
Node's `fs.watchFile` is a polling watcher under the hood — this is the
same shape: stat-mtime once per second, callback on change. No external
deps, no platform quirks.

Cache semantics: values computed lazily via `get(key, compute_fn)`. Cache
hit returns the stored value; on dirty, recomputes under a lock. The
dirty flag is cleared BEFORE compute starts so a change that lands
mid-compute re-dirties the entry and the next read picks it up.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("whisper-studio")

_WATCH_INTERVAL_SECONDS = 1.0


@dataclass
class _CacheEntry:
    value: Any
    dirty: bool
    compute: Callable[[], Any]


class GitFileWatcher:
    """Singleton watcher for the workspace's .git directory.

    Watches HEAD, config, and the loose ref for the current branch.
    Fires registered subscribers (the SSE endpoint) when any change.
    Maintains a lazy cache that callers read via `get(key, compute)`.
    """

    def __init__(self) -> None:
        self._workspace: str | None = None
        self._git_dir: str | None = None
        self._common_dir: str | None = None
        self._branch_ref_path: str | None = None
        # mtimes for every watched path; None means "didn't exist last poll"
        self._mtimes: dict[str, float | None] = {}
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.Lock()
        self._subscribers: list[Callable[[], None]] = []
        self._subscribers_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="GitFileWatcher",
            daemon=True,
        )
        self._thread.start()
        log.info("GitFileWatcher started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        log.info("GitFileWatcher stopped")

    def set_workspace(self, workspace: str | None) -> None:
        """Point the watcher at a new workspace. Clears cache + re-resolves paths."""
        with self._cache_lock:
            self._cache.clear()
        self._workspace = workspace
        self._refresh_watched_paths()

    # ── Subscriber API ─────────────────────────────────────────────

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback fired when any watched file changes.

        Returns an unsubscribe function. Callbacks run on the watcher
        thread — keep them short (e.g., put-into-queue, then return).
        """
        with self._subscribers_lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._subscribers_lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    # ── Cache API ──────────────────────────────────────────────────

    def get(self, key: str, compute: Callable[[], Any]) -> Any:
        """Return cached value or recompute if dirty.

        Race condition handling: dirty is cleared BEFORE compute starts.
        A file change arriving during compute re-dirties the entry so the
        next get() reads fresh instead of serving a stale value.
        """
        with self._cache_lock:
            existing = self._cache.get(key)
            if existing is not None and not existing.dirty:
                return existing.value
            # Pre-clear dirty so concurrent invalidations re-set it
            if existing is not None:
                existing.dirty = False
            else:
                self._cache[key] = _CacheEntry(value=None, dirty=False, compute=compute)

        value = compute()

        with self._cache_lock:
            entry = self._cache.get(key)
            # Only update the value if no invalidation arrived mid-compute
            if entry is not None and not entry.dirty:
                entry.value = value
                entry.compute = compute
            elif entry is None:
                self._cache[key] = _CacheEntry(value=value, dirty=False, compute=compute)
        return value

    def invalidate(self) -> None:
        """Mark every cached entry dirty. Does not recompute."""
        with self._cache_lock:
            for entry in self._cache.values():
                entry.dirty = True

    # ── Internal: path resolution ──────────────────────────────────

    def _refresh_watched_paths(self) -> None:
        """Resolve .git, commondir, and the current branch ref. Idempotent."""
        from server.git.filesystem import (
            get_common_dir,
            read_git_head,
            resolve_git_dir,
        )

        self._git_dir = None
        self._common_dir = None
        self._branch_ref_path = None
        self._mtimes = {}

        if not self._workspace:
            return

        git_dir = resolve_git_dir(self._workspace)
        if not git_dir:
            return
        self._git_dir = git_dir
        self._common_dir = get_common_dir(git_dir)

        # Always watch HEAD and config
        self._mtimes[os.path.join(git_dir, "HEAD")] = self._safe_mtime(
            os.path.join(git_dir, "HEAD")
        )
        config_dir = self._common_dir or git_dir
        self._mtimes[os.path.join(config_dir, "config")] = self._safe_mtime(
            os.path.join(config_dir, "config")
        )

        # Watch the current branch's ref file
        head = read_git_head(git_dir)
        refs_dir = self._common_dir or git_dir
        if head and head.get("type") == "branch":
            ref_path = os.path.join(refs_dir, "refs", "heads", head["name"])
            self._branch_ref_path = ref_path
            self._mtimes[ref_path] = self._safe_mtime(ref_path)

    @staticmethod
    def _safe_mtime(path: str) -> float | None:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    # ── Internal: poll loop ────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_for_changes()
            except Exception:
                log.exception("GitFileWatcher poll cycle failed")
            self._stop_event.wait(_WATCH_INTERVAL_SECONDS)

    def _check_for_changes(self) -> None:
        if not self._git_dir or not self._workspace:
            return

        # Determine which (if any) paths changed
        changed = False
        head_changed = False
        for path, prev in list(self._mtimes.items()):
            current = self._safe_mtime(path)
            if current != prev:
                changed = True
                if path.endswith(os.sep + "HEAD") or path.endswith("/HEAD"):
                    head_changed = True
                self._mtimes[path] = current

        if not changed:
            return

        # If HEAD changed, the current branch may have flipped — re-resolve
        # the branch ref path and start watching the new one. This mirrors
        # Claude Code's watchCurrentBranchRef logic.
        if head_changed:
            self._rewatch_branch_ref()

        self.invalidate()
        self._notify_subscribers()

    def _rewatch_branch_ref(self) -> None:
        from server.git.filesystem import read_git_head

        if not self._git_dir:
            return
        # Stop watching the old branch ref
        if self._branch_ref_path and self._branch_ref_path in self._mtimes:
            del self._mtimes[self._branch_ref_path]
        self._branch_ref_path = None

        head = read_git_head(self._git_dir)
        if head and head.get("type") == "branch":
            refs_dir = self._common_dir or self._git_dir
            ref_path = os.path.join(refs_dir, "refs", "heads", head["name"])
            self._branch_ref_path = ref_path
            # watchFile semantics: tracking a not-yet-existing file is fine
            self._mtimes[ref_path] = self._safe_mtime(ref_path)

    def _notify_subscribers(self) -> None:
        with self._subscribers_lock:
            callbacks = list(self._subscribers)
        for cb in callbacks:
            try:
                cb()
            except Exception:
                log.exception("GitFileWatcher subscriber callback raised")


# Module-level singleton
git_watcher = GitFileWatcher()


__all__ = ["GitFileWatcher", "git_watcher"]
