"""Foreground wait with live hand-off to background — the anti-restart fix.

The old flow ran a read-only command with ``run_sandboxed(timeout=30)``, which
KILLED the process group on timeout, then re-ran the same command from zero as
a background task: the first 30 seconds of work were discarded, and any side
effects ran twice.

``run_with_handoff`` starts the process exactly once (``popen_sandboxed``
streaming to the task output file), waits up to the foreground budget, and on
timeout registers the LIVE process in the task registry and hands it to the
shell waiter — zero work lost, zero double execution.
"""

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass

from server.tasks import registry, shell
from server.tasks.events import emit_task_event

log = logging.getLogger("whisper-studio")

FOREGROUND_BUDGET_S = 30


@dataclass
class HandoffResult:
    background: bool
    returncode: int | None = None
    output: str = ""
    task_id: str | None = None
    output_path: str | None = None


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
    """
    from server.sandbox import popen_sandboxed

    task_id = uuid.uuid4().hex[:12]
    out_path = shell.output_path_for(task_id)

    out_file = open(out_path, "w")
    try:
        proc, profile_path = popen_sandboxed(exec_command, cwd=cwd, stdout_file=out_file)
    finally:
        if not out_file.closed:
            out_file.close()

    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.info("Handing off to background after %ss: %s", timeout, command[:80])
        registry.create_task(
            "shell",
            session_id=session_id,
            title=command,
            command=command,
            output_path=out_path,
            meta={"cwd": cwd, "handoff": True},
            task_id=task_id,
        )
        registry.attach_pid(task_id, proc.pid)
        # Announce BEFORE the waiter takes over, so a process that exits
        # right after the handoff cannot complete ahead of its start event.
        task = registry.get_task(task_id)
        if task:
            emit_task_event(session_id, "task_started", task)
        shell.adopt_running_process(task_id, proc, out_path, session_id, profile_path)
        return HandoffResult(background=True, task_id=task_id, output_path=out_path)

    # Finished inline: read output back, clean up every trace.
    if profile_path:
        try:
            os.unlink(profile_path)
        except OSError:
            pass
    try:
        with open(out_path, encoding="utf-8", errors="replace") as f:
            output = f.read()
    except OSError:
        output = ""
    try:
        os.unlink(out_path)
    except OSError:
        pass
    return HandoffResult(background=False, returncode=returncode, output=output)
