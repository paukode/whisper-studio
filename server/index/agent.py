"""macOS launchd agent: refresh indexed workspaces on their own schedules even
when the app isn't running.

Each workspace carries its own schedule + a "refresh when closed" flag in its
index settings (server/index/wssettings). ``sync()`` writes one LaunchAgent plist
that wakes at the union of the chosen hours across the folders that opted in; on
wake the worker re-indexes every workspace that is DUE by its own cadence — but
only when the app itself isn't running (the app's in-process scheduler covers
that case), so the two never index the same workspace at once. launchd runs a
calendar wake missed while the Mac was asleep/off when it next wakes.
"""

from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
import time

from . import paths, store, wssettings

log = logging.getLogger("whisper-studio")

_LABEL = "com.whisperstudio.indexrefresh"
_PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{_LABEL}.plist")
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_VENV_PY = os.path.join(_REPO_DIR, "venv", "bin", "python")
_STORAGE = os.path.dirname(paths.INDEX_DATA_DIR)
_PID_PATH = os.path.join(_STORAGE, ".server.pid")
_LOG_PATH = os.path.join(_STORAGE, "index_agent.log")
_PY_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ── app-running detection (PID file written by the server lifespan) ──────────
def mark_app_running() -> None:
    try:
        os.makedirs(_STORAGE, exist_ok=True)
        with open(_PID_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception:  # noqa: BLE001
        pass


def mark_app_stopped() -> None:
    try:
        os.remove(_PID_PATH)
    except Exception:  # noqa: BLE001
        pass


def _app_running() -> bool:
    try:
        with open(_PID_PATH) as f:
            pid = int(f.read().strip())
    except Exception:  # noqa: BLE001
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ── which folders want background refresh, and at which hours ─────────────────
def _opted_in_workspaces() -> list[str]:
    out = []
    for ws in store.list_indexed_workspaces():
        try:
            s = wssettings.get_settings(ws)
        except Exception:  # noqa: BLE001
            continue
        if s["refresh_when_closed"] and s["schedule"]["enabled"]:
            out.append(ws)
    return out


def _wake_intervals() -> list[dict]:
    """Distinct wake hours across opted-in folders, as launchd calendar entries.
    Wakes daily at each hour; the worker's cadence check gates weekly/every-N."""
    hours = set()
    for ws in _opted_in_workspaces():
        try:
            hours.add(int(wssettings.get_settings(ws)["schedule"]["hour"]))
        except Exception:  # noqa: BLE001
            pass
    return [{"Hour": h, "Minute": 0} for h in sorted(hours)]


# ── plist / (un)install ───────────────────────────────────────────────────────
def _plist_dict(intervals: list[dict]) -> dict:
    return {
        "Label": _LABEL,
        "ProgramArguments": [_VENV_PY, "-m", "server.index.agent", "run"],
        "WorkingDirectory": _REPO_DIR,
        "StartCalendarInterval": intervals,
        "StandardOutPath": _LOG_PATH,
        "StandardErrorPath": _LOG_PATH,
        "RunAtLoad": False,
    }


def is_installed() -> bool:
    return os.path.exists(_PLIST_PATH)


def _launchctl(*args: str) -> None:
    try:
        subprocess.run(["launchctl", *args], check=False, capture_output=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning("launchctl %s failed: %s", args, e)


def _write_and_load(intervals: list[dict]) -> None:
    os.makedirs(os.path.dirname(_PLIST_PATH), exist_ok=True)
    with open(_PLIST_PATH, "wb") as f:
        plistlib.dump(_plist_dict(intervals), f)
    _launchctl("unload", _PLIST_PATH)
    _launchctl("load", _PLIST_PATH)
    log.info("Index refresh agent installed for hours %s", [i["Hour"] for i in intervals])


def uninstall() -> dict:
    if os.path.exists(_PLIST_PATH):
        _launchctl("unload", _PLIST_PATH)
        try:
            os.remove(_PLIST_PATH)
        except Exception:  # noqa: BLE001
            pass
        log.info("Index refresh agent removed")
    return {"installed": False, "supported": sys.platform == "darwin"}


def sync() -> dict:
    """Install/refresh the agent if any folder opted in (wake at their hours), or
    remove it if none did. Called whenever a workspace's settings change. macOS
    only."""
    if sys.platform != "darwin":
        return {"installed": False, "supported": False}
    intervals = _wake_intervals()
    if not intervals:
        return uninstall()
    _write_and_load(intervals)
    return {"installed": True, "supported": True}


def regenerate() -> None:
    """Best-effort sync used by the scheduler hook on any settings change."""
    try:
        sync()
    except Exception as e:  # noqa: BLE001
        log.warning("Index agent sync failed: %s", e)


def status() -> dict:
    return {"installed": is_installed(), "supported": sys.platform == "darwin"}


# ── the worker (run by launchd) ──────────────────────────────────────────────
def _due(sched: dict, last: float, now: float) -> bool:
    freq = sched.get("frequency", "daily")
    if last <= 0:
        return True
    if freq == "every_n_days":
        n = max(1, int(sched.get("interval_days", 2)))
        return (now - last) >= (n * 86400 - 3600)
    if freq == "weekly":
        if (now - last) >= (7 * 86400 - 3600):
            return True  # catch up a missed week
        from datetime import datetime

        target = _PY_WEEKDAY.get(sched.get("weekday", "mon"), 0)
        return datetime.now().weekday() == target and (now - last) >= 20 * 3600
    return (now - last) >= 20 * 3600  # daily: at most once a day


def run() -> int:
    """launchd entry point: refresh every opted-in workspace that's due, unless
    the app is running (it handles its own refresh)."""
    if _app_running():
        log.info("Index agent: app is running — leaving the refresh to it.")
        return 0
    now = time.time()
    refreshed = 0
    for ws in store.list_indexed_workspaces():
        try:
            # Only touch folders with an index under the ACTIVE embed backend; a
            # store.get_meta() read below opens (and would fabricate) that backend's
            # db, so skip folders indexed only under the other embedder.
            if not store.has_index(ws):
                continue
            s = wssettings.get_settings(ws)
            if not (s["refresh_when_closed"] and s["schedule"]["enabled"]):
                continue
            last = float(store.get_meta(ws).get("last_auto_refresh", 0) or 0)
            if not _due(s["schedule"], last, now):
                continue
            from .pipeline import build  # heavy imports only when actually refreshing

            log.info("Index agent: refreshing %s", ws)
            build(ws)
            store.set_meta(ws, last_auto_refresh=now)
            refreshed += 1
        except Exception as e:  # noqa: BLE001 — one bad workspace must not stop the rest
            log.error("Index agent: refresh failed for %s: %s", ws, e)
    log.info("Index agent: refreshed %d workspace(s).", refreshed)
    return 0


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(prog="server.index.agent")
    parser.add_argument("action", choices=["sync", "uninstall", "status", "run"])
    action = parser.parse_args().action
    if action == "sync":
        print(sync())
    elif action == "uninstall":
        print(uninstall())
    elif action == "status":
        print(status())
    else:
        sys.exit(run())
