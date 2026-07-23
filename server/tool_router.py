"""
Tool router — pure dispatch layer.

Maps tool names to handler functions. No lifecycle management (hooks, permissions,
input cloning, abort) — that belongs in tool_executor.py.

Each route returns (output_string, side_effects_list) where side_effects are dicts
ready for ndjson_dumps.
"""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("whisper-studio")

# Side effect keys with special meaning for the executor layer
SIDE_EFFECT_PAUSE = "_pause"  # Signals stream should pause (ask_user_question)


def _submit(loop: asyncio.AbstractEventLoop, executor: ThreadPoolExecutor, fn):
    """run_in_executor with contextvars propagation.

    Plain run_in_executor drops the calling context, which would silently
    break the per-agent workspace override (worktree isolation) the moment a
    tool body runs on the thread pool. Copying the context at submission
    keeps get_workspace_path() honest on worker threads.
    """
    import contextvars

    ctx = contextvars.copy_context()
    return loop.run_in_executor(executor, ctx.run, fn)


async def route_tool(
    tool_name: str,
    tool_input: dict,
    *,
    loop: asyncio.AbstractEventLoop,
    executor: ThreadPoolExecutor,
    transcript: str,
    attachments: dict | None,
    session_id: str,
    model_id: str,
    tool_use_id: str,
    origin: str = "chat",
    effort_label: str | None = None,
) -> tuple[str, list[dict]]:
    """Dispatch a tool call to its handler.

    Returns:
        (output, side_effects) where side_effects is a list of dicts.
        Each side effect dict is an SSE event payload (e.g. {"todo_update": ...}).
        A side effect with key "_pause" signals the stream should pause.
    """
    side_effects: list[dict] = []

    # --- MCP tools ---
    from server.mcp import mcp_manager

    if mcp_manager.is_mcp_tool(tool_name):
        from server.skills import execute_mcp_tool

        output = await execute_mcp_tool(tool_name, tool_input)
        return output, side_effects

    # --- Live preview tools ---
    # Async branch (not @register_executor's sync/thread-pool path) — the
    # Playwright objects a preview session owns must stay bound to this one
    # running event loop.
    from server.preview.tools import PREVIEW_TOOL_NAMES

    if tool_name in PREVIEW_TOOL_NAMES:
        from server.preview.router import execute_preview_tool

        output = await execute_preview_tool(tool_name, tool_input)
        return output, side_effects

    # --- Task tools ---
    if tool_name in ("task_create", "task_update", "task_list", "task_get", "task_stop"):
        from server.tasks_tracker import execute_task_tool, get_session_tasks

        tool_input["session_id"] = session_id  # Always use real session ID
        output = await _submit(
            loop, executor, lambda ci=tool_input: execute_task_tool(tool_name, ci)
        )
        side_effects.append({"todo_update": get_session_tasks(session_id)})
        return output, side_effects

    # --- Background-task tools (unified registry; distinct from the todo
    # tracker's task_* family above) ---
    if tool_name in ("task_status", "task_output", "task_cancel"):
        from server.tasks.tools import execute_background_task_tool

        output = await _submit(
            loop,
            executor,
            lambda ci=tool_input: execute_background_task_tool(tool_name, ci, session_id),
        )
        return output, side_effects

    # --- Cron tools ---
    if tool_name in ("cron_create", "cron_update", "cron_run", "cron_list", "cron_delete"):
        from server.cron_scheduler import execute_cron_tool

        output = await _submit(
            loop,
            executor,
            lambda ci=tool_input: execute_cron_tool(tool_name, ci, session_id=session_id),
        )
        return output, side_effects

    # --- LSP tools ---
    if tool_name in ("lsp_diagnostics", "lsp_hover", "lsp_references"):
        from server.lsp import execute_lsp_tool

        output = await _submit(
            loop, executor, lambda ci=tool_input: execute_lsp_tool(tool_name, ci)
        )
        return output, side_effects

    # --- Worktree tools ---
    if tool_name in ("ws_create_worktree", "ws_diff_worktree", "ws_merge_worktree"):
        from server.skills import execute_tool

        output = await _submit(
            loop,
            executor,
            lambda ci=tool_input: execute_tool(tool_name, ci, transcript, attachments),
        )
        return output, side_effects

    # --- Sleep ---
    if tool_name == "sleep":
        secs = min(float(tool_input.get("seconds", 3)), 30)
        side_effects.append({"skill": "sleep", "input": {"seconds": secs}})
        await asyncio.sleep(secs)
        output = f"Slept for {secs} seconds."
        side_effects.append({"skill_result": "sleep", "output": output})
        return output, side_effects

    # --- Ask user question ---
    if tool_name == "ask_user_question":
        question_text = tool_input.get("question", "")
        options = tool_input.get("options", [])
        side_effects.append(
            {
                "user_question": {
                    "question": question_text,
                    "options": options,
                    "tool_use_id": tool_use_id,
                }
            }
        )
        side_effects.append({SIDE_EFFECT_PAUSE: True})
        output = "[PAUSE] Waiting for user to answer the question. The user's reply will arrive as their next message."
        return output, side_effects

    # --- create_artifact: emit a self-contained artifact card ---
    # (SSE key stays `program_artifact` — internal, decoupled from the tool name.)
    if tool_name == "create_artifact":
        title = tool_input.get("title", "Untitled Artifact")
        html = tool_input.get("html", "")
        description = tool_input.get("description", "")
        side_effects.append(
            {
                "program_artifact": {
                    "title": title,
                    "html": html,
                    "description": description,
                    "tool_use_id": tool_use_id,
                }
            }
        )
        output = f"Artifact '{title}' added to the chat. The user can preview and download it."
        return output, side_effects

    # --- create_plan: persist a plan doc + emit a plan card ---
    # Mirrors create_artifact: the full markdown goes to data/plans/ (not the
    # chat), and a `plan_generated` side-effect renders a compact card that
    # opens the plan in the dock.
    if tool_name == "create_plan":
        from server.plans.store import write_plan

        title = tool_input.get("title", "Plan")
        summary = tool_input.get("summary", "")
        markdown = tool_input.get("markdown", "")
        saved = await _submit(
            loop,
            executor,
            lambda: write_plan(session_id, title, markdown),
        )
        side_effects.append(
            {
                "plan_generated": {
                    "id": saved["id"],
                    "title": title,
                    "summary": summary,
                    "tool_use_id": tool_use_id,
                }
            }
        )
        output = (
            f"Plan '{title}' saved and opened in the side pane. "
            "Now reply with ONLY the one-line summary — do not repeat the plan here."
        )
        return output, side_effects

    # --- Workspace open folder ---
    if tool_name == "ws_open_folder":
        from server.workspace import execute_ws_open_folder

        result = await _submit(loop, executor, lambda ci=tool_input: execute_ws_open_folder(ci))
        try:
            parsed = json.loads(result)
            if parsed.get("__ws_switch__"):
                side_effects.append({"ws_folder_opened": parsed["__ws_switch__"]})
        except Exception:
            pass
        return result, side_effects

    # --- Notebook tools ---
    from server.notebook import NOTEBOOK_TOOL_NAMES, execute_notebook_tool

    if tool_name in NOTEBOOK_TOOL_NAMES:
        output = await _submit(
            loop, executor, lambda ci=tool_input: execute_notebook_tool(tool_name, ci)
        )
        return output, side_effects

    # --- Config tools ---
    if tool_name == "config_get":
        from server.agent_tools import execute_config_get

        output = await _submit(loop, executor, lambda ci=tool_input: execute_config_get(ci))
        return output, side_effects

    if tool_name == "config_set":
        from server.agent_tools import execute_config_set

        output = await _submit(loop, executor, lambda ci=tool_input: execute_config_set(ci))
        return output, side_effects

    # --- Skill list ---
    if tool_name == "skill_list":
        from server.agent_tools import execute_skill_list

        output = await _submit(loop, executor, execute_skill_list)
        return output, side_effects

    # --- Skill invoke (agent-powered) ---
    if tool_name == "skill_invoke":
        from server.agents.runtime import run_agent

        skill_name = tool_input.get("skill_name", "")
        skill_input_text = tool_input.get("input", "")
        result = await run_agent(
            f"Use the {skill_name} skill to: {skill_input_text}",
            agent_type="general",
            session_id=session_id,
            model_id_override=model_id,
        )
        output = f"[Skill Agent Result]\n{result.output}"
        return output, side_effects

    # --- Tool search ---
    if tool_name == "tool_search":
        from server.agent_tools import execute_tool_search
        from server.chat.tool_pool import assemble_full_catalog
        from server.workspace import get_workspace_path

        # The TRUE full catalog (pre-partition, post nothing): the old
        # hand-rolled union silently missed plan/result-cache/preview/
        # worktree/background-task tools. Deferred tools found here are
        # ACTIVATED for this session so they're callable next round.
        all_t = assemble_full_catalog(
            plan_mode=False,
            ws_connected=bool(get_workspace_path()),
            mcp_enabled_names=None,
        )
        output = execute_tool_search(tool_input, all_t, session_id=session_id)
        return output, side_effects

    # --- MCP resources ---
    if tool_name == "list_mcp_resources":
        from server.agent_tools import execute_list_mcp_resources

        output = await _submit(loop, executor, lambda ci=tool_input: execute_list_mcp_resources(ci))
        return output, side_effects

    if tool_name == "read_mcp_resource":
        from server.agent_tools import execute_read_mcp_resource

        output = await execute_read_mcp_resource(tool_input)
        return output, side_effects

    # --- Notify user ---
    if tool_name == "notify_user":
        side_effects.append(
            {
                "notify_user": {
                    "message": tool_input.get("message", ""),
                    "status": tool_input.get("status", "normal"),
                    "title": tool_input.get("title", ""),
                }
            }
        )
        # Durable record behind the header bell — the toast alone is lost on
        # a 5s timer if nobody is looking. Best-effort by design.
        try:
            from server.notifications import record_notification

            record_notification(
                session_id=session_id,
                source=origin,
                title=tool_input.get("title", ""),
                message=tool_input.get("message", ""),
                status=tool_input.get("status", "normal"),
            )
        except Exception as e:
            log.warning("notify_user: durable record failed: %s", e)
        return "Notification sent to user.", side_effects

    # --- Workflow tools (ultracode runtime) ---
    if tool_name == "workflow_run":
        from server.workflows.tools import execute_workflow_run

        output, extra = await execute_workflow_run(tool_input, session_id, model_id, effort_label)
        side_effects.extend(extra)
        return output, side_effects

    if tool_name == "workflow_status":
        from server.workflows.tools import execute_workflow_status

        return execute_workflow_status(tool_input, session_id), side_effects

    if tool_name == "workflow_save":
        from server.workflows.tools import execute_workflow_save

        return await execute_workflow_save(tool_input), side_effects

    if tool_name == "workflow_list":
        from server.workflows.tools import execute_workflow_list

        return execute_workflow_list(tool_input, session_id), side_effects

    # --- CI tools (WS-J: watch + autofix) ---
    if tool_name == "ci_watch":
        from server.ci.tools import execute_ci_watch

        output, extra = await execute_ci_watch(tool_input, session_id)
        side_effects.extend(extra)
        return output, side_effects

    if tool_name == "ci_status":
        from server.ci.tools import execute_ci_status

        return await execute_ci_status(tool_input, session_id), side_effects

    if tool_name == "ci_autofix":
        from server.ci.tools import execute_ci_autofix

        output, extra = await execute_ci_autofix(tool_input, session_id, model_id)
        side_effects.extend(extra)
        return output, side_effects

    # --- Agent tools ---
    if tool_name == "spawn_agent":
        from server.agent_tools import execute_spawn_agent

        output = await execute_spawn_agent(
            tool_input, session_id, model_id, effort_label=effort_label
        )
        return output, side_effects

    if tool_name == "send_message":
        from server.agent_tools import execute_send_message

        output = execute_send_message(tool_input)
        return output, side_effects

    if tool_name == "list_agents":
        from server.agent_tools import execute_list_agents

        output = execute_list_agents(session_id)
        return output, side_effects

    if tool_name == "team_create":
        from server.agent_tools import execute_team_create

        output, team_payload = await execute_team_create(
            tool_input, model_id, session_id, effort_label=effort_label
        )
        side_effects.append({"team_results": team_payload})
        return output, side_effects

    if tool_name == "team_delete":
        from server.agent_tools import execute_team_delete

        output = await _submit(loop, executor, lambda ci=tool_input: execute_team_delete(ci))
        return output, side_effects

    # --- Memory tools ---
    from server.memory.tools import MEMORY_TOOL_NAMES

    if tool_name in MEMORY_TOOL_NAMES:
        from server.executors import EXECUTORS

        _mem_fn = EXECUTORS.get(tool_name)
        if _mem_fn:
            output = await _submit(loop, executor, lambda ci=tool_input: _mem_fn(ci))
        else:
            output = f"Unknown memory tool: {tool_name}"
        return output, side_effects

    # --- Default: skill/executor fallback ---
    from server.skills import execute_tool

    output = await _submit(
        loop,
        executor,
        lambda ci=tool_input: execute_tool(tool_name, ci, transcript, attachments),
    )
    return output, side_effects
