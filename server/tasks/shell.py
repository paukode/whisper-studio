"""Registry-backed background shell runner.

Replaces the in-memory dict of server/background_tasks.py: every background
command is a row in the ``agent_tasks`` registry (persistent, reconciled on
boot) and announces its lifecycle into the owning session via
:func:`server.tasks.events.emit_task_event`.

Process handles cannot be persisted, so live ``Popen`` objects stay in a
module-level map; the registry row is the durable source of truth for status.
Commands run under the same OS sandbox profile as foreground execution
(``popen_sandboxed``) — previously background tasks silently escaped the
sandbox that foreground commands ran under.
"""

import logging
import os
import subprocess
import threading

from server.infrastructure.paths import data_root
from server.tasks import registry
from server.tasks.events import emit_task_event
from server.tasks.registry import _tail_of_file

log = logging.getLogger("whisper-studio")

OUTPUT_DIR = os.path.join(data_root(), "background_output")

_procs: dict[str, subprocess.Popen] = {}
_profiles: dict[str, str | None] = {}
_stopped: set[str] = set()
_lock = threading.Lock()

_STATUS_EVENT = {
    "completed": "task_completed",
    "failed": "task_failed",
    "stopped": "task_stopped",
}


def output_path_for(task_id: str) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    return os.path.join(OUTPUT_DIR, f"{task_id}.txt")


def start_shell_task(
    command: str,
    *,
    cwd: str,
    session_id: str = "",
    exec_command: str | None = None,
) -> dict:
    """Start ``command`` in the background; returns {task_id, output_path, status}.

    ``exec_command`` is the possibly-wrapped form actually executed (cwd
    tracking, snapshot wrapping); ``command`` is what the user asked for and
    becomes the task title.
    """
    import uuid

    from server.sandbox import popen_sandboxed

    task_id = uuid.uuid4().hex[:12]
    out_path = output_path_for(task_id)
    registry.create_task(
        "shell",
        session_id=session_id,
        title=command,
        command=command,
        output_path=out_path,
        meta={"cwd": cwd},
        task_id=task_id,
    )

    out_file = open(out_path, "w")
    try:
        proc, profile_path = popen_sandboxed(exec_command or command, cwd=cwd, stdout_file=out_file)
    except Exception as e:
        out_file.close()
        finished = registry.finish_task(
            task_id, status="failed", exit_code=-1, result_text=f"[Error: {e}]"
        )
        if finished:
            emit_task_event(session_id, "task_failed", finished)
        return {"task_id": task_id, "output_path": out_path, "status": "failed"}
    finally:
        # The child holds its own descriptor; the parent copy must not leak.
        if not out_file.closed:
            out_file.close()

    registry.attach_pid(task_id, proc.pid)
    # Announce BEFORE handing to the waiter: a fast-exiting command must not
    # get its completion event delivered ahead of its start event.
    task = registry.get_task(task_id)
    if task:
        emit_task_event(session_id, "task_started", task)
    adopt_running_process(task_id, proc, out_path, session_id, profile_path)
    return {"task_id": task_id, "output_path": out_path, "status": "running"}


def adopt_running_process(
    task_id: str,
    proc: subprocess.Popen,
    output_path: str,
    session_id: str,
    profile_path: str | None = None,
) -> None:
    """Register a live process against an existing registry row and watch it.

    Used both by ``start_shell_task`` and by the foreground->background
    handoff (server/tasks/handoff.py), which spawns the process itself.
    """
    with _lock:
        _procs[task_id] = proc
        _profiles[task_id] = profile_path
    threading.Thread(
        target=_waiter,
        args=(task_id, proc, output_path, session_id),
        daemon=True,
        name=f"task-shell-{task_id}",
    ).start()


def _waiter(task_id: str, proc: subprocess.Popen, output_path: str, session_id: str) -> None:
    try:
        exit_code = proc.wait()
    except Exception as e:  # pragma: no cover — wait() failing is exotic
        log.error("tasks.shell: wait failed for %s: %s", task_id, e)
        exit_code = -1
    with _lock:
        was_stopped = task_id in _stopped
        _stopped.discard(task_id)
        _procs.pop(task_id, None)
        profile_path = _profiles.pop(task_id, None)
    if profile_path:
        try:
            os.unlink(profile_path)
        except OSError:
            pass
    if was_stopped:
        status = "stopped"
    else:
        status = "completed" if exit_code == 0 else "failed"
    finished = registry.finish_task(
        task_id,
        status=status,
        exit_code=exit_code,
        result_text=_tail_of_file(output_path),
    )
    if finished:
        emit_task_event(session_id, _STATUS_EVENT[status], finished)


def stop_task(task_id: str) -> bool:
    """Kill a running background task's process group.

    Returns True if a kill signal was sent. In-process tasks are killed via
    their live Popen handle and the waiter records status 'stopped'.
    Re-adopted tasks (surviving a server restart) have no Popen — those fall
    back to the registry row's process-group-leader pid: kill the group,
    close the row first-wins (the adoption watcher's later 'completed'
    transition then no-ops), and emit the stop event here.
    """
    with _lock:
        proc = _procs.get(task_id)
        if proc is not None:
            if proc.poll() is not None:
                return False
            _stopped.add(task_id)
    if proc is not None:
        from server.process_utils import kill_process_group

        kill_process_group(proc)
        return True

    # Adopted-task fallback: no handle, but the row knows the group leader.
    task = registry.get_task(task_id)
    if not task or task.get("status") != "running" or not task.get("pid"):
        return False
    import signal

    try:
        os.killpg(os.getpgid(int(task["pid"])), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    finished = registry.finish_task(
        task_id,
        status="stopped",
        exit_code=None,
        result_text=_tail_of_file(task.get("output_path")),
    )
    if finished:
        emit_task_event(finished.get("session_id") or "", "task_stopped", finished)
    return True


def stop_session_tasks(session_id: str) -> list[str]:
    """Stop every running background task started by the given session (ESC).

    Candidates come from the REGISTRY, not the in-memory handle map, so
    re-adopted tasks (which have no Popen after a server restart) are
    covered too.
    """
    if not session_id:
        return []
    stopped = []
    for task in registry.list_tasks(session_id=session_id, status="running", kind="shell"):
        if stop_task(task["task_id"]):
            stopped.append(task["task_id"])
    return stopped
