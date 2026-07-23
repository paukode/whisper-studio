"""Poll a branch's latest Actions run to a terminal conclusion.

Pure and injectable: it calls the :mod:`server.ci.provider` functions (which
tests monkeypatch) and an ``on_event`` sink for live ticks. Each poll runs the
blocking ``gh`` calls off the event loop via ``asyncio.to_thread``. Bounded by
``max_polls`` so a hung run can't watch forever; cancellable via ``cancel_event``.
"""

from __future__ import annotations

import asyncio
import logging

from server.ci import provider

log = logging.getLogger("whisper-studio")

DEFAULT_POLL_INTERVAL = 20
DEFAULT_MAX_POLLS = 90  # 20s * 90 = 30 min ceiling
_RESOLVE_RETRIES = 6  # a just-pushed branch may have no run for a few seconds


async def watch_branch(
    branch: str,
    *,
    cwd: str,
    on_event=None,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
    max_polls: int = DEFAULT_MAX_POLLS,
    cancel_event: asyncio.Event | None = None,
    sleep=asyncio.sleep,
) -> dict:
    """Watch ``branch``'s newest run; return a terminal outcome dict.

    outcome keys: found, run_id, status, conclusion, url, jobs, failed_jobs,
    failing (bool), timed_out (bool), cancelled (bool), polls.
    """
    emit = on_event or (lambda ev: None)

    run = None
    for attempt in range(_RESOLVE_RETRIES):
        if _is_cancelled(cancel_event):
            return _outcome(None, cancelled=True)
        run = await asyncio.to_thread(provider.latest_run, branch, cwd)
        if run and run.get("run_id") is not None:
            break
        # Don't sleep after the final attempt — we're about to give up.
        if attempt < _RESOLVE_RETRIES - 1:
            await sleep(poll_interval)
    if not run or run.get("run_id") is None:
        emit({"type": "ci_progress", "status": "no_run", "branch": branch})
        return _outcome(None, found=False)

    run_id = run["run_id"]
    for poll in range(max_polls):
        if _is_cancelled(cancel_event):
            return _outcome(run, cancelled=True, polls=poll)
        full = await asyncio.to_thread(provider.get_run, run_id, cwd)
        run = full or run
        emit(
            {
                "type": "ci_progress",
                "run_id": run_id,
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "workflow": run.get("workflow"),
                "url": run.get("url"),
                "jobs": run.get("jobs", []),
                "poll": poll,
            }
        )
        if provider.is_terminal(run):
            return _outcome(run, polls=poll + 1)
        await sleep(poll_interval)

    return _outcome(run, timed_out=True, polls=max_polls)


def _is_cancelled(ev: asyncio.Event | None) -> bool:
    return ev is not None and ev.is_set()


def _outcome(
    run: dict | None,
    *,
    found: bool = True,
    timed_out: bool = False,
    cancelled: bool = False,
    polls: int = 0,
) -> dict:
    if not run:
        return {
            "found": found,
            "run_id": None,
            "status": None,
            "conclusion": None,
            "url": None,
            "jobs": [],
            "failed_jobs": [],
            "failing": False,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "polls": polls,
        }
    return {
        "found": True,
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "url": run.get("url"),
        "workflow": run.get("workflow"),
        "jobs": run.get("jobs", []),
        "failed_jobs": provider.failed_jobs(run),
        "failing": provider.is_failing(run),
        "timed_out": timed_out,
        "cancelled": cancelled,
        "polls": polls,
    }
