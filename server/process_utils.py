"""
Process group management utilities.

Provides helpers to spawn subprocesses in their own process group
and kill the entire group gracefully (SIGTERM → wait → SIGKILL).
"""

import asyncio
import logging
import os
import signal
import subprocess

log = logging.getLogger("whisper-studio")

GRACEFUL_TIMEOUT = 10  # seconds between SIGTERM and SIGKILL


def new_process_group():
    """Pre-exec function to place child in its own process group."""
    os.setpgrp()


def kill_process_group(process: subprocess.Popen, timeout: int = GRACEFUL_TIMEOUT):
    """Kill a process and its entire process group gracefully.

    Sends SIGTERM to the process group, waits up to *timeout* seconds,
    then sends SIGKILL if still alive.
    """
    pid = process.pid
    if pid is None:
        return

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return  # Already dead

    # SIGTERM the whole group
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return

    # Wait for graceful shutdown
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass

    # SIGKILL the whole group
    try:
        os.killpg(pgid, signal.SIGKILL)
        log.warning("Process group %d did not exit after SIGTERM, sent SIGKILL", pgid)
    except (ProcessLookupError, OSError):
        pass

    # Reap
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.error("Process group %d still alive after SIGKILL", pgid)


async def kill_process_group_async(
    process: "asyncio.subprocess.Process", timeout: int = GRACEFUL_TIMEOUT
):
    """Async-safe sibling of kill_process_group() for asyncio-spawned
    subprocesses. asyncio.subprocess.Process.wait() takes no timeout kwarg
    (unlike subprocess.Popen.wait()), so the sync helper above cannot be
    reused directly — this re-implements the identical
    SIGTERM -> wait -> SIGKILL -> reap sequence using asyncio.wait_for."""
    pid = process.pid
    if pid is None:
        return

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return  # Already dead

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
        return
    except asyncio.TimeoutError:
        pass

    try:
        os.killpg(pgid, signal.SIGKILL)
        log.warning("Process group %d did not exit after SIGTERM, sent SIGKILL", pgid)
    except (ProcessLookupError, OSError):
        pass

    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        log.error("Process group %d still alive after SIGKILL", pgid)
