"""Poll a branch's latest Actions run to a terminal conclusion.

Pure and injectable: it calls the :mod:`server.ci.provider` functions (which
tests monkeypatch) and an ``on_event`` sink for live ticks. Each poll runs the
blocking ``gh`` calls off the event loop via ``asyncio.to_thread``. Bounded by
``max_polls`` so a hung run can't watch forever; cancellable via ``cancel_event``.

A gh failure on one poll (network blip, rate limit, timeout) is logged and the
watch keeps polling; ``_MAX_GH_FAILURES`` failures in a row end the watch by
raising the last :class:`provider.GhError`, which the manager records as a
failed watch with gh's message on the card.
"""

from __future__ import annotations

import asyncio
import logging

from server.ci import provider

log = logging.getLogger("whisper-studio")

DEFAULT_POLL_INTERVAL = 20
DEFAULT_MAX_POLLS = 90  # 20s * 90 = 30 min ceiling
_RESOLVE_RETRIES = 6  # a just-pushed branch may have no run for a few seconds
_MAX_GH_FAILURES = 3  # gh failures in a row before the watch fails


class _GhFailures:
    """Counts gh failures in a row; the next success resets the count."""

    def __init__(self) -> None:
        self.count = 0

    async def call(self, fn, *args):
        """``fn(*args)`` off the loop. A GhError returns None until it is the
        ``_MAX_GH_FAILURES``-th in a row, which re-raises it."""
        try:
            result = await asyncio.to_thread(fn, *args)
        except provider.GhError as e:
            self.count += 1
            if self.count >= _MAX_GH_FAILURES:
                raise
            log.warning("ci watch: gh failed (%d in a row): %s", self.count, e)
            return None
        self.count = 0
        return result


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
    gh = _GhFailures()

    run = None
    for attempt in range(_RESOLVE_RETRIES):
        if _is_cancelled(cancel_event):
            return _outcome(None, cancelled=True)
        run = await gh.call(provider.latest_run, branch, cwd)
        if run and run.get("run_id") is not None:
            break
        # Don't sleep after the final attempt, we're about to give up.
        if attempt < _RESOLVE_RETRIES - 1:
            await sleep(poll_interval)
    if gh.count:
        # The last lookup failed: the branch's runs are unknown, not absent.
        raise provider.GhError(f"gh could not list the runs for {branch}")
    if not run or run.get("run_id") is None:
        emit({"type": "ci_progress", "status": "no_run", "branch": branch})
        return _outcome(None, found=False)

    run_id = run["run_id"]
    for poll in range(max_polls):
        if _is_cancelled(cancel_event):
            return _outcome(run, cancelled=True, polls=poll)
        full = await gh.call(provider.get_run, run_id, cwd)
        if full is None:
            # This poll's gh call failed; keep the last known run and try again.
            await sleep(poll_interval)
            continue
        run = full
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
