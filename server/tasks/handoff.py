"""Foreground wait with live hand-off to background — the anti-restart fix.

The old flow ran a read-only command with ``run_sandboxed(timeout=30)``, which
KILLED the process group on timeout, then re-ran the same command from zero as
a background task: the first 30 seconds of work were discarded, and any side
effects ran twice.

``run_with_handoff`` starts the process exactly once (``popen_sandboxed``
streaming to the task output file), waits up to the foreground budget, and on
timeout registers the LIVE process in the task registry and hands it to the
shell waiter — zero work lost, zero double execution.

While a command waits on its budget it has no registry row yet, so it is also
tracked here per session from the moment it spawns. ``stop_turn_work`` (the
Stop and ESC kill switch) reaches both kinds: the foreground commands and the
background tasks the stopped turn started.

A command that finishes inline but leaves work running in its process group
(``npm run dev &``, ``nohup ... &``: the shell returns, the server keeps its
port) is not forgotten either: that group becomes a registry row of its own,
so Stop, the task panel, task_cancel and the runtime cap all still reach it.
"""

import logging
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from server.tasks import owner as work_owner
from server.tasks import registry, shell
from server.tasks.events import emit_task_event

log = logging.getLogger("whisper-studio")

FOREGROUND_BUDGET_S = 30

# Foreground commands still inside their budget, by owning session:
# {session_id: {pid: (proc, spawned_at, owner)}}, where ``owner`` names the
# run that started it ("" for the chat turn, see server/tasks/owner.py). The
# same lock makes "hand off to a registry row" and "Stop collects its
# targets" mutually exclusive, so a command is always visible to Stop as
# exactly one of the two.
_fg_lock = threading.Lock()
_foreground: dict[str, dict[int, tuple[subprocess.Popen, float, str]]] = {}
# Pids a Stop killed while they were still in the foreground: never adopted
# as a background task afterwards, even when the budget expires mid-kill.
_fg_stopped: set[int] = set()

_STOPPED_NOTE = "[stopped: the turn that started this command was stopped]"

# After an inline finish, how long the leader's group gets to empty before
# what is left counts as work the command left running. Covers members that
# are merely finishing, so a normal command never becomes a task row.
_LINGER_GRACE_S = 0.25


@dataclass
class HandoffResult:
    """``background`` True: the command itself outlived its budget and runs on
    as task ``task_id``. ``background`` False with a ``task_id``: the command
    finished inline (``returncode``, ``output``) but left processes running
    in its group, tracked as that task."""

    background: bool
    returncode: int | None = None
    output: str = ""
    task_id: str | None = None
    output_path: str | None = None
    # A session Stop killed the command before it finished.
    stopped: bool = False


def run_with_handoff(
    command: str,
    exec_command: str,
    *,
    cwd: str,
    session_id: str = "",
    timeout: float = FOREGROUND_BUDGET_S,
) -> HandoffResult:
    """Run ``exec_command`` foreground-first with a single spawn.

    Finished within ``timeout``: returns the combined output inline (stderr
    merged into stdout, same merge background tasks always did) and leaves no
    registry row behind.

    Still running at ``timeout``: inserts a registry row around the live
    process, hands it to the shell waiter, and returns the background handle.

    Finished, but processes it backgrounded are still running in its group:
    returns inline as above, and that group becomes a registry row (the
    result's ``task_id``) exactly like a handed-off command.

    Stopped by the session's Stop while still in the foreground: returns
    inline with whatever it printed, and is never handed off.
    """
    from server.process_utils import wait_group_gone
    from server.sandbox import popen_sandboxed

    task_id = uuid.uuid4().hex[:12]
    out_path = shell.output_path_for(task_id)

    spawned_at = time.time()
    owner = work_owner.current()
    out_file = open(out_path, "w")
    try:
        proc, profile_path = popen_sandboxed(exec_command, cwd=cwd, stdout_file=out_file)
    finally:
        if not out_file.closed:
            out_file.close()

    _track(session_id, proc, spawned_at, owner)
    left_running: HandoffResult | None = None
    try:
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            handed = _hand_off(
                command, proc, session_id, cwd, task_id, out_path, profile_path, spawned_at, owner
            )
            if handed is not None:
                return handed
            # A Stop is killing it right now; its kill escalates, so this ends.
            returncode = proc.wait()
        if not wait_group_gone(proc, _LINGER_GRACE_S):
            # The shell returned but left work running in its group. Still
            # under the foreground lock discipline: either a Stop already
            # claimed the group (and is killing all of it), or it becomes a
            # task row before it stops being tracked here.
            left_running = _hand_off(
                command,
                proc,
                session_id,
                cwd,
                task_id,
                out_path,
                profile_path,
                spawned_at,
                owner,
                left_running=True,
            )
    finally:
        stopped = _untrack(session_id, proc)

    output = _read_output(out_path)
    if left_running is not None:
        # The group's task owns the output file and the sandbox profile now.
        return HandoffResult(
            background=False,
            returncode=returncode,
            output=output,
            task_id=left_running.task_id,
            output_path=left_running.output_path,
        )

    # Finished inline: clean up every trace.
    if profile_path:
        try:
            os.unlink(profile_path)
        except OSError:
            pass
    try:
        os.unlink(out_path)
    except OSError:
        pass
    if stopped:
        output = f"{output.rstrip()}\n{_STOPPED_NOTE}" if output.strip() else _STOPPED_NOTE
    return HandoffResult(background=False, returncode=returncode, output=output, stopped=stopped)


def _read_output(out_path: str) -> str:
    try:
        with open(out_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _track(session_id: str, proc: subprocess.Popen, spawned_at: float, owner: str) -> None:
    with _fg_lock:
        _foreground.setdefault(session_id, {})[proc.pid] = (proc, spawned_at, owner)


def _untrack_locked(session_id: str, proc: subprocess.Popen) -> None:
    procs = _foreground.get(session_id)
    if procs is not None:
        procs.pop(proc.pid, None)
        if not procs:
            _foreground.pop(session_id, None)


def _untrack(session_id: str, proc: subprocess.Popen) -> bool:
    """Forget a finished foreground command; True when a Stop had killed it."""
    with _fg_lock:
        _untrack_locked(session_id, proc)
        if proc.pid in _fg_stopped:
            _fg_stopped.discard(proc.pid)
            return True
    return False


def _hand_off(
    command: str,
    proc: subprocess.Popen,
    session_id: str,
    cwd: str,
    task_id: str,
    out_path: str,
    profile_path: str | None,
    spawned_at: float,
    owner: str,
    *,
    left_running: bool = False,
) -> HandoffResult | None:
    """Move a live foreground command into the task registry, or None when a
    Stop already claimed it. ``left_running``: the command itself finished and
    the row tracks the work it left running in its process group."""
    with _fg_lock:
        if proc.pid in _fg_stopped:
            return None
        meta = {"cwd": cwd, "handoff": True, "spawned_at": spawned_at}
        if owner:
            meta["owner"] = owner
        if left_running:
            meta["left_running"] = True
            log.info("Tracking work left running by: %s", command[:80])
        else:
            log.info("Handing off to background: %s", command[:80])
        registry.create_task(
            "shell",
            session_id=session_id,
            title=command,
            command=command,
            output_path=out_path,
            meta=meta,
            task_id=task_id,
        )
        registry.attach_pid(task_id, proc.pid)
        # Announce BEFORE the waiter takes over, so a process that exits
        # right after the handoff cannot complete ahead of its start event.
        task = registry.get_task(task_id)
        if task:
            emit_task_event(session_id, "task_started", task)
        shell.adopt_running_process(task_id, proc, out_path, session_id, profile_path)
        _untrack_locked(session_id, proc)
    return HandoffResult(background=True, task_id=task_id, output_path=out_path)


def _started_at(task: dict) -> float | None:
    """When the work behind a registry row began: the spawn time a handoff
    recorded, else the row's creation time."""
    spawned = (task.get("meta") or {}).get("spawned_at")
    if isinstance(spawned, (int, float)):
        return float(spawned)
    created = task.get("created_at") or ""
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def stop_turn_work(session_id: str, since: float) -> dict:
    """Stop what the session's stopped chat turn started: every foreground
    command and background shell task the chat began at or after ``since``
    (epoch seconds, when that turn's stream started). Work from earlier turns,
    such as a dev server started before, keeps running; the task panel stops
    that. So does work of another run that shares the session id (a detached
    agent, a workflow, a voice-delegated run, a cron job, a /subagent run;
    see server/tasks/owner.py), even when it started in the same window.

    Every target is signalled at once and the kills run in parallel threads,
    so one process that ignores SIGTERM does not hold up the rest. Blocking:
    call it off the event loop. Returns ``{"stopped": [task ids],
    "foreground": <count>}``.
    """
    return _stop_matching(
        session_id,
        lambda owner, started: not owner and started >= since,
        f"Stop for session {session_id}",
    )


def stop_owned_work(session_id: str, owner: str) -> dict:
    """Stop every foreground command and background shell task ``owner`` (a
    run named through server/tasks/owner.run_owned) started in the session,
    whenever it started. For a run that was stopped: cancelling its coroutine
    cannot stop a command running in a worker thread. Blocking; returns what
    ``stop_turn_work`` returns."""
    if not owner:
        return {"stopped": [], "foreground": 0}
    return _stop_matching(
        session_id,
        lambda o, _started: o == owner,
        f"Stop of {owner} in session {session_id}",
    )


def _stop_matching(session_id: str, match, label: str) -> dict:
    """Kill the session's foreground commands and running shell tasks for
    which ``match(owner, started_at)`` holds."""
    if not session_id:
        return {"stopped": [], "foreground": 0}
    from server.process_utils import group_running, kill_process_group

    with _fg_lock:
        # group_running, not the leader alone: a command whose shell just
        # returned may still hold backgrounded work that is about to become
        # a task row; Stop claims the whole group either way.
        fg = [
            proc
            for proc, spawned_at, owner in (_foreground.get(session_id) or {}).values()
            if match(owner, spawned_at) and group_running(proc)
        ]
        _fg_stopped.update(p.pid for p in fg)
        task_ids = [
            t["task_id"]
            for t in registry.list_tasks(session_id=session_id, status="running", kind="shell")
            if match(str((t.get("meta") or {}).get("owner") or ""), _started_at(t) or 0)
        ]
    if not fg and not task_ids:
        return {"stopped": [], "foreground": 0}

    with ThreadPoolExecutor(max_workers=min(16, len(fg) + len(task_ids))) as pool:
        for proc in fg:
            pool.submit(kill_process_group, proc)
        results = list(zip(task_ids, pool.map(shell.stop_task, task_ids), strict=True))
    stopped = [tid for tid, ok in results if ok]
    log.info("%s: %d foreground command(s), %d background task(s)", label, len(fg), len(stopped))
    return {"stopped": stopped, "foreground": len(fg)}
