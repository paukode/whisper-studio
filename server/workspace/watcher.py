"""The file tree follows the disk: a watcher on the connected workspace.

``watchfiles`` (the Rust notify crate: FSEvents on macOS, inotify on Linux)
watches the connected folder recursively on a daemon thread. Every batch of
raw changes is reduced to the folders whose listing may differ and the files
whose content changed (every entry counts: the listing hides nothing, so a
change inside .git or node_modules matters once that folder is open), and
published as ONE app-wide
``workspace_changed`` event on the session-events channel. The UI re-lists
only the loaded folders the event names and re-reads only the open tabs it
names (src/stores/workspaceStore.ts, reloadDirs).

Why a watcher: the tree is lazy. The refresh button re-listed the root and
kept every expanded folder's children as they were, so a file the model
created inside an open folder never showed until the workspace was
reconnected, however often the button was pressed.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable

from watchfiles import Change

log = logging.getLogger("whisper-studio")

# A batch closes 150 ms after its last change, or 600 ms after its first.
_STEP_MS = 150
_DEBOUNCE_MS = 600
# The watch loop checks its stop flag this often.
_RUST_TIMEOUT_MS = 1000
# Past this many entries the event names none, and the UI reloads all it has.
_MAX_NAMED = 200


def coalesce(root: str, changes: Iterable[tuple[Change, str]]) -> dict | None:
    """One ``workspace_changed`` payload for a batch of (change, path) pairs,
    or None when nothing visible changed.

    ``dirs``: workspace-relative folders whose listing may differ ("" is the
    root): the parent of every added or deleted entry, and a folder whose own
    entry was modified. ``files``: files whose content changed; an added file
    counts too, since a save that writes a temp file and renames it over the
    old one arrives as an add. Both sorted; either is None when too long to
    name.
    """
    dirs: set[str] = set()
    files: set[str] = set()
    for change, path in changes:
        rel = os.path.relpath(path, root)
        if rel == "." or rel.startswith(".."):
            continue
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            files.add(rel)
            if change != Change.modified:
                dirs.add(os.path.dirname(rel))
        elif os.path.isdir(full):
            dirs.add(rel if change == Change.modified else os.path.dirname(rel))
        else:
            # Gone: deleted, or renamed away. Its parent lists differently.
            dirs.add(os.path.dirname(rel))
    if not dirs and not files:
        return None
    return {
        "type": "workspace_changed",
        "root": root,
        "dirs": sorted(dirs) if len(dirs) <= _MAX_NAMED else None,
        "files": sorted(files) if len(files) <= _MAX_NAMED else None,
    }


def _publish_app_wide(payload: dict) -> None:
    from server.agents.event_bus import event_bus

    event_bus.publish("", payload)


class WorkspaceWatcher:
    """Singleton: one watch thread for the connected workspace.

    ``set_workspace`` may be called from any thread (the connect endpoint, a
    tool that clones a repo). It never joins the old thread: the old watch
    loop sees its stop flag within _RUST_TIMEOUT_MS and exits, and it checks
    that flag before it publishes, so nothing from the old folder follows a
    switch.
    """

    def __init__(self, publish: Callable[[dict], None] = _publish_app_wide) -> None:
        self._publish = publish
        self._root: str | None = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._lock = threading.Lock()

    @property
    def root(self) -> str | None:
        return self._root

    def set_workspace(self, root: str | None) -> None:
        """Watch ``root``; None stops. A watch already running on ``root`` stays."""
        real = os.path.realpath(root) if root else None
        with self._lock:
            if real == self._root and self._thread is not None and self._thread.is_alive():
                return
            self._stop_locked()
            self._root = real
            if real is None or not os.path.isdir(real):
                return
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(
                target=self._run, args=(real, stop), name="WorkspaceWatcher", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
            self._root = None

    def _stop_locked(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._stop = None
        self._thread = None

    def _run(self, root: str, stop: threading.Event) -> None:
        from watchfiles import watch

        log.info("WorkspaceWatcher watching %s", root)
        try:
            for changes in watch(
                root,
                watch_filter=None,
                step=_STEP_MS,
                debounce=_DEBOUNCE_MS,
                stop_event=stop,
                rust_timeout=_RUST_TIMEOUT_MS,
                raise_interrupt=False,
            ):
                if stop.is_set():
                    break
                payload = coalesce(root, changes)
                if payload is None:
                    continue
                log.debug(
                    "workspace_changed: %s dirs, %s files",
                    "many" if payload["dirs"] is None else len(payload["dirs"]),
                    "many" if payload["files"] is None else len(payload["files"]),
                )
                self._publish(payload)
        except Exception:
            log.exception("WorkspaceWatcher stopped watching %s", root)
        log.info("WorkspaceWatcher left %s", root)


workspace_watcher = WorkspaceWatcher()
