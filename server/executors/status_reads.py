"""Read-only executors for the run status tools a tool script may call.

``workflow_status``, ``workflow_list`` and ``session_search`` are dispatched by
name in server/tool_router.py when the model calls them, so they had no entry
in the executor registry. The tool script runtime
(server/workflows/runtime.WorkflowRun._handle_tool) runs only tools the
registry marks read-only, and it dispatches through server.skills.execute_tool,
which reads the registry. Without an entry here a script that read a run's
status was refused as "not read-only", although these tools only read.

Each executor is a thin seam over the function the router already calls, so
a direct call and a script call return the same text. ``task_status`` and
``task_output`` are registered read-only in server/tasks/tools.py.
"""

from __future__ import annotations

from server.executors import register_executor


def _session(tool_input: dict) -> str:
    return str(tool_input.pop("__session_id__", "") or "")


@register_executor("workflow_status", read_only=True)
def _exec_workflow_status(tool_input, transcript, current_attachments):
    from server.workflows.tools import execute_workflow_status

    session_id = _session(tool_input)
    return execute_workflow_status(tool_input, session_id)


@register_executor("workflow_list", read_only=True)
def _exec_workflow_list(tool_input, transcript, current_attachments):
    from server.workflows.tools import execute_workflow_list

    session_id = _session(tool_input)
    return execute_workflow_list(tool_input, session_id)


@register_executor("session_search", read_only=True)
def _exec_session_search(tool_input, transcript, current_attachments):
    from server.agent_tools import execute_session_search

    session_id = _session(tool_input)
    return execute_session_search(tool_input, session_id)
