"""Programmatic tool calling (PTC): run one model-written JS program that
orchestrates READ-ONLY tools through a `tools` binding, so a tool-heavy task
runs as ONE turn with every intermediate result kept in the program's
variables instead of the model's context.

The program runs in the same hardened Node harness the workflow runtime uses
(server/workflows/harness), so it inherits vm isolation, the scrubbed env, and
`--disallow-code-generation-from-strings`. Each `tools.<name>(args)` call RPCs
back to the parent, which runs the tool through the real executor pipeline
behind a read-only gate (server/workflows/runtime.WorkflowRun._handle_tool):
a write or approval-requiring tool called from a script is refused, so a script
can never mutate anything or pause for an approval card. Bulky sub-results are
spilled into the run journal while the program keeps the full value.

Why read-only only: an interactive approval card cannot be shown mid-program
without wedging the script, so writes stay OUT of scope by design (call those
directly as normal tools). This is the honest boundary, not a temporary gap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from server.executors import register_executor

log = logging.getLogger("whisper-studio")

# Wall-clock ceiling for one script. Generous enough for a real multi-file
# sweep, bounded so a runaway loop still ends.
_SCRIPT_TIMEOUT_S = 180
_MAX_RESULT_CHARS = 100_000


async def do_run_tool_script(payload: dict) -> tuple[bool, str]:
    """Run a previously-approved tool script and return (ok, output). The
    script's `return` value becomes the output; anything it printed via log()
    is folded in as context lines."""
    source = (payload.get("code") or "").strip()
    if not source:
        return False, "code is required"

    from server.workflows.runtime import WorkflowRun

    async def _no_agent(prompt, opts):  # noqa: ARG001
        raise RuntimeError(
            "agent() is unavailable in run_tool_script — it orchestrates read-only "
            "tools, not model calls. Use tools.<name>(args), or call spawn_agent "
            "directly as a normal tool."
        )

    # The workflow harness requires a `meta` literal; supply one so the model
    # writes a plain body ending in `return`.
    wrapped = (
        'export const meta = {name:"tool_script",'
        'description:"programmatic read-only tool calling"}\n' + source
    )
    run_id = "toolscript-" + uuid.uuid4().hex[:12]

    from server.workspace import get_workspace_path

    run = WorkflowRun(
        run_id,
        wrapped,
        session_id=(payload.get("session_id") or ""),
        model_key=(payload.get("model_key") or ""),
        model_id=(payload.get("model_id") or ""),
        workspace_path=get_workspace_path(),
        agent_runner=_no_agent,
    )
    try:
        outcome = await asyncio.wait_for(run.run(), timeout=_SCRIPT_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            await run.cancel()
        except Exception:
            pass
        return False, f"tool script timed out after {_SCRIPT_TIMEOUT_S}s"
    except Exception as e:  # noqa: BLE001
        return False, f"tool script failed to run: {e}"

    if outcome.get("status") != "done":
        return False, outcome.get("error") or "tool script did not complete"

    result = outcome.get("result")
    if result is None:
        text = "(script returned nothing — end it with `return <value>`)"
    elif isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        except Exception:
            text = str(result)
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n... (truncated)"
    return True, text


@register_executor("run_tool_script", read_only=False, concurrent_safe=False)
def _exec_run_tool_script(tool_input, transcript, current_attachments):
    """Emit an approval request. The script runs only after the user approves
    it (or 'Yes, all cli' is set) — the SCRIPT is approved once; its individual
    read-only tool calls are not separately gated."""
    session_id = tool_input.pop("__session_id__", "")
    code = (tool_input.get("code") or "").strip()
    if not code:
        return "Error: code is required."
    payload = json.dumps({"action": "run_tool_script", "code": code, "session_id": session_id})
    return f"[WS_APPROVAL]{payload}"
