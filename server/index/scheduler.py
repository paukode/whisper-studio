"""Per-workspace index refresh scheduler.

Each indexed workspace carries its own refresh schedule (daily / every N days /
weekly on a weekday, at an hour) in its index settings (server/index/wssettings).
This installs one APScheduler job per workspace whose schedule is enabled, and
re-applies a workspace's job whenever its settings change. Builds run in daemon
threads so the event loop is never blocked.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import datetime, timedelta

from . import store

log = logging.getLogger("whisper-studio")

_DEFAULT_HOUR = 7
_JOB_PREFIX = "index_refresh_"

_scheduler = None


def _job_id(ws_path: str) -> str:
    norm = os.path.abspath(os.path.expanduser(ws_path)).encode()
    return _JOB_PREFIX + hashlib.sha1(norm).hexdigest()[:16]


def _build_one(ws_path: str) -> None:
    try:
        from .pipeline import build

        log.info("Scheduled index refresh: %s", ws_path)
        build(ws_path)
    except Exception as e:  # noqa: BLE001
        log.error("Scheduled index refresh failed for %s: %s", ws_path, e)


def _run_one(ws_path: str) -> None:
    # Offload the (multi-minute) build to a daemon thread so the scheduler loop
    # returns immediately, mirroring cron_scheduler's pattern.
    threading.Thread(target=_build_one, args=(ws_path,), daemon=True, name="index-refresh").start()


def apply_workspace(ws_path: str) -> None:
    """(Re)install this workspace's refresh job from its settings. No-op when the
    scheduler is absent."""
    if _scheduler is None:
        return
    from . import wssettings

    jid = _job_id(ws_path)
    try:
        _scheduler.remove_job(jid)
    except Exception:
        pass
    sch = wssettings.get_settings(ws_path)["schedule"]
    if not sch.get("enabled"):
        return
    hour = int(sch.get("hour", _DEFAULT_HOUR))
    freq = sch.get("frequency", "daily")
    if freq == "weekly":
        _scheduler.add_job(
            _run_one,
            "cron",
            args=[ws_path],
            day_of_week=sch.get("weekday", "mon"),
            hour=hour,
            minute=0,
            id=jid,
            replace_existing=True,
        )
    elif freq == "every_n_days":
        n = max(1, int(sch.get("interval_days", 2)))
        # Anchor to `hour`; if that moment passed today, start next occurrence so
        # the first run isn't immediate.
        anchor = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
        if anchor < datetime.now():
            anchor += timedelta(days=n)
        _scheduler.add_job(
            _run_one,
            "interval",
            args=[ws_path],
            days=n,
            start_date=anchor,
            id=jid,
            replace_existing=True,
        )
    else:  # daily
        _scheduler.add_job(
            _run_one, "cron", args=[ws_path], hour=hour, minute=0, id=jid, replace_existing=True
        )
    log.info("Index refresh scheduled for %s (%s at %02d:00)", ws_path, freq, hour)


def apply_all() -> None:
    """Re-install jobs for every indexed workspace (called at startup)."""
    for ws in store.list_indexed_workspaces():
        try:
            apply_workspace(ws)
        except Exception as e:  # noqa: BLE001
            log.warning("apply_workspace failed for %s: %s", ws, e)


async def init_index_scheduler() -> None:
    """Start the index scheduler at app startup (best-effort)."""
    global _scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:
        log.info("APScheduler not installed — scheduled index refresh disabled")
        return
    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    apply_all()
