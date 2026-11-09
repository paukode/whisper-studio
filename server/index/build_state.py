"""The one record of workspace index builds in this process.

Per workspace root: ``{building, done, total, current, error}``. Every build
claims its workspace here first, whoever starts it (the Build button, the
scheduled refresh, a direct ``pipeline.build``), so:

  - two builds never race the same index's SQLite writes;
  - ``/build`` refuses and ``/status`` reports "building" from the same fact;
  - a scheduled build shows its progress like a manual one.

The build itself (``pipeline.build`` / ``pipeline.start_build``) reports
progress and its outcome through ``note``. Cancel is cooperative: a
per-workspace Event the build loop checks between files.
"""

from __future__ import annotations

import os
import threading

IDLE = {"building": False, "done": 0, "total": 0, "current": None, "error": None}

_builds: dict[str, dict] = {}
_cancel_events: dict[str, threading.Event] = {}
_guard = threading.Lock()


def ws_root(ws_path: str) -> str:
    return os.path.abspath(os.path.expanduser(ws_path))


def cancel_event(root: str) -> threading.Event:
    with _guard:
        ev = _cancel_events.get(root)
        if ev is None:
            ev = _cancel_events[root] = threading.Event()
        return ev


def claim(root: str) -> bool:
    """Mark a build of ``root`` as running; False when one already is. A
    stale cancel is cleared here, at the claim, so a cancel that lands
    between the claim and the first file is kept."""
    ev = cancel_event(root)
    with _guard:
        if _builds.get(root, IDLE)["building"]:
            return False
        _builds[root] = {**IDLE, "building": True}
        ev.clear()
        return True


def note(root: str, **fields) -> None:
    """Record progress, or the end of the build (``building=False`` with its
    ``error``)."""
    with _guard:
        st = _builds.get(root)
        if st is not None:
            st.update(fields)


def status(ws_path: str) -> dict:
    """This workspace's build state: whether a build runs, its progress, and
    the error of the last build that failed (None when it succeeded)."""
    with _guard:
        return dict(_builds.get(ws_root(ws_path), IDLE))


def is_building(ws_path: str) -> bool:
    """True while a build of this workspace runs, whoever started it."""
    return status(ws_path)["building"]


def forget(ws_path: str) -> None:
    """Drop a finished build's record (its index was removed). A build that
    is still running keeps its record: it is still building."""
    root = ws_root(ws_path)
    with _guard:
        if not _builds.get(root, IDLE)["building"]:
            _builds.pop(root, None)


def request_cancel(ws_path: str) -> bool:
    """Ask an in-progress build for this workspace to stop after the current
    file. Returns True if a build was running to signal."""
    if not is_building(ws_path):
        return False
    cancel_event(ws_root(ws_path)).set()
    return True
