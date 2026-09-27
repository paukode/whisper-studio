"""
Process group management utilities.

Provides helpers to spawn subprocesses in their own process group
and kill the entire group gracefully (SIGTERM → wait → SIGKILL).

Every caller spawns the child as a group leader (``new_process_group`` below,
or ``start_new_session`` for the asyncio preview spawn), so the leader's pid IS
the group id. The kill targets that group id directly instead of asking the
kernel for the leader's group: once the leader has exited (a shell that
backgrounded ``npm run dev &`` and returned), ``getpgid`` on it fails while
the rest of the group is still running, and those children used to survive
every timeout and Stop. The group id cannot be handed to an unrelated process
while any member is alive (POSIX never gives a new process a pid equal to an
active process group id), so signalling it is safe for as long as it answers.
"""

import asyncio
import logging
import os
import signal
import subprocess
import time

log = logging.getLogger("whisper-studio")

GRACEFUL_TIMEOUT = 10  # seconds between SIGTERM and SIGKILL
_REAP_TIMEOUT = 5  # seconds to wait for the group to vanish after SIGKILL
_GROUP_POLL_S = 0.05


def new_process_group():
    """Pre-exec function to place child in its own process group."""
    os.setpgrp()


def group_alive(pgid: int) -> bool:
    """Does any process still belong to group ``pgid``?

    ``PermissionError`` counts as gone: macOS answers EPERM for a group whose
    only member is an unreaped zombie, and a group we may not signal is not
    one of ours to wait on.
    """
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _signal_group(pgid: int, sig: int) -> bool:
    """Send ``sig`` to the group. False when the group is already gone."""
    try:
        os.killpg(pgid, sig)
    except OSError:
        return False
    return True


def _group_id(process) -> int | None:
    pid = getattr(process, "pid", None)
    return int(pid) if pid else None


def kill_process_group(process: subprocess.Popen, timeout: int = GRACEFUL_TIMEOUT):
    """Kill a process and its entire process group gracefully.

    Sends SIGTERM to the process group, waits up to *timeout* seconds for
    EVERY member to exit (not just the leader, which may already be gone),
    then sends SIGKILL to whatever is left. Blocking: call it off the event
    loop.
    """
    pgid = _group_id(process)
    if pgid is None:
        return
    if not _signal_group(pgid, signal.SIGTERM):
        process.poll()
        return  # nothing left in the group

    if wait_group_gone(process, timeout):
        return

    if _signal_group(pgid, signal.SIGKILL):
        log.warning("Process group %d did not exit after SIGTERM, sent SIGKILL", pgid)
    if not wait_group_gone(process, _REAP_TIMEOUT):
        log.error("Process group %d still alive after SIGKILL", pgid)


def group_running(process: subprocess.Popen) -> bool:
    """Is anything of ``process``'s group still running: the leader, or work
    it left behind in the group after it exited (``npm run dev &``)?"""
    if process.poll() is None:
        return True
    pgid = _group_id(process)
    return pgid is not None and group_alive(pgid)


def wait_group_gone(
    process: subprocess.Popen, timeout: float, poll_s: float = _GROUP_POLL_S
) -> bool:
    """Poll until the whole group has exited, reaping the leader on the way
    (an unreaped leader would keep the group alive as a zombie on Linux).
    True when it is gone within ``timeout`` seconds. Blocking."""
    pgid = _group_id(process)
    if pgid is None:
        return True
    deadline = time.monotonic() + timeout
    while True:
        process.poll()
        if not group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


async def kill_process_group_async(
    process: "asyncio.subprocess.Process", timeout: int = GRACEFUL_TIMEOUT
):
    """Async-safe sibling of kill_process_group() for asyncio-spawned
    subprocesses: the identical SIGTERM, wait for the whole group, SIGKILL,
    reap sequence, with asyncio sleeps instead of blocking waits. The SIGTERM
    goes out before the first await, so a caller cancelled mid-kill has at
    worst skipped the SIGKILL escalation, never the signal itself."""
    pgid = _group_id(process)
    if pgid is None:
        return
    if not _signal_group(pgid, signal.SIGTERM):
        await _settle_returncode(process)
        return

    if await _wait_group_gone_async(pgid, timeout):
        await _settle_returncode(process)
        return

    if _signal_group(pgid, signal.SIGKILL):
        log.warning("Process group %d did not exit after SIGTERM, sent SIGKILL", pgid)
    if not await _wait_group_gone_async(pgid, _REAP_TIMEOUT):
        log.error("Process group %d still alive after SIGKILL", pgid)
    await _settle_returncode(process)


async def _settle_returncode(process) -> None:
    """Let asyncio record the leader's exit so ``returncode`` (and so
    ``DevServerProcess.alive``) is current when the kill returns."""
    wait = getattr(process, "wait", None)
    if wait is None or getattr(process, "returncode", None) is not None:
        return
    try:
        await asyncio.wait_for(wait(), timeout=1)
    except (asyncio.TimeoutError, OSError):
        pass


async def _wait_group_gone_async(pgid: int, timeout: float) -> bool:
    # asyncio's child watcher reaps the leader itself, so only group
    # membership is polled here.
    deadline = time.monotonic() + timeout
    while True:
        if not group_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_GROUP_POLL_S)
