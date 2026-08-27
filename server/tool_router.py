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
    from server.tasks.tools import BACKGROUND_TASK_TOOL_NAMES

    if tool_name in BACKGROUND_TASK_TOOL_NAMES:
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

    # --- create_visual / create_chart: inline diagram or chart card ---
    # Both emit the same `viz_artifact` side effect (one card, two kinds) and
    # neither is approval-gated: nothing is written and nothing runs on the
    # host — the chart spec is evaluated in a sandboxed iframe.
    if tool_name in ("create_visual", "create_chart"):
        from server.visuals import check_svg_geometry, validate_chart_spec, validate_svg

        title = tool_input.get("title", "Untitled")
        description = tool_input.get("description", "")

        if tool_name == "create_visual":
            svg = tool_input.get("svg", "")
            # Geometry problems bounce at most once per session (the resubmit
            # renders regardless), so a sloppy model gets one repair round and
            # can never retry-loop.
            error = validate_svg(svg) or check_svg_geometry(svg, session_id)
            if error:
                return error, side_effects
            payload = {"kind": "svg", "source": svg}
        else:
            spec, error = validate_chart_spec(tool_input.get("spec"))
            if error:
                return error, side_effects
            payload = {"kind": "chart", "source": json.dumps(spec)}

        side_effects.append(
            {
                "viz_artifact": {
                    **payload,
                    "title": title,
                    "description": description,
                    "tool_use_id": tool_use_id,
                }
            }
        )
        noun = "Diagram" if tool_name == "create_visual" else "Chart"
        return (
            f"{noun} '{title}' is now rendered in the chat, with download and copy "
            "buttons. Do not repeat its contents; add at most a short sentence of "
            "context.",
            side_effects,
        )

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
            # Same as spawn_agent and team_create below: the skill's agent runs
            # at the turn's effort. Omitting it ran skills at no effort at all.
            effort_label=effort_label,
        )
        output = f"[Skill Agent Result]\n{result.output}"
        return output, side_effects

    # --- Tool search ---
    if tool_name == "tool_search":
        from server.agent_tools import execute_tool_search
        from server.chat.tool_pool import assemble_full_catalog
        from server.infrastructure.effort import is_ultracode
        from server.workspace import get_workspace_path

        # The TRUE full catalog (pre-partition): every registered tool,
        # including plan/result-cache/preview/worktree/background-task tools.
        # Deferred tools found here are ACTIVATED for this session so they're
        # callable next round. ultracode rides along so the catalog matches the
        # round's own — searching for "workflow" from an ultracode turn must not
        # come back empty for tools the very same turn can call.
        all_t = assemble_full_catalog(
            plan_mode=False,
            ws_connected=bool(get_workspace_path()),
            ultracode=is_ultracode(effort_label),
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
        message = tool_input.get("message", "")
        side_effects.append(
            {
                "notify_user": {
                    "message": message,
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
                message=message,
                status=tool_input.get("status", "normal"),
            )
        except Exception as e:
            log.warning("notify_user: durable record failed: %s", e)
        # Carry the delivered message in the tool_result itself instead of a
        # generic ack. The message previously lived ONLY in the side-effect
        # dict above, invisible to the model's own transcript and to any
        # evaluator reading it (server.goals.tail.render_tail renders
        # tool_result content into the judged tail for both the interactive
        # completion gate and cron's own verify-and-continue) — a plain
        # "sent." made a genuinely-delivered report look like missing work.
        if message.strip():
            return f"Notification sent to user: {message}", side_effects
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

        return await execute_workflow_save(tool_input, session_id), side_effects

    if tool_name == "workflow_delete":
        from server.workflows.tools import execute_workflow_delete

        return execute_workflow_delete(tool_input), side_effects

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

        output, extra = await execute_ci_autofix(tool_input, session_id, model_id, effort_label)
        side_effects.extend(extra)
        return output, side_effects

    # --- Agent tools ---
    # spawn_agent / send_message / receive_messages / complete_coordination
    # are dispatched from an agent's OWN turn too (server.agents.runtime's
    # unattended inner loop, ported onto this shared executor). Read the
    # ambient nesting context (None outside an agent's own loop, e.g.
    # interactive chat) so a nested call threads the calling agent's real
    # id/depth/team/event-channel through instead of behaving as a fresh,
    # unrelated top-level spawn every time.
    if tool_name in ("spawn_agent", "send_message", "receive_messages", "complete_coordination"):
        from server.agents.runtime import agent_nesting_ctx

        _nesting = agent_nesting_ctx.get()
    else:
        _nesting = None

    if tool_name == "spawn_agent":
        from server.agent_tools import execute_spawn_agent

        output = await execute_spawn_agent(
            tool_input,
            session_id,
            model_id,
            effort_label=effort_label,
            parent_agent_id=_nesting["agent_id"] if _nesting else None,
            depth=_nesting["depth"] if _nesting else 0,
            team_id=_nesting["team_id"] if _nesting else None,
            event_channel=_nesting["event_channel"] if _nesting else None,
        )
        return output, side_effects

    if tool_name == "send_message":
        from server.agent_tools import execute_send_message

        output = execute_send_message(
            tool_input, from_id=_nesting["agent_id"] if _nesting else "main"
        )
        return output, side_effects

    if tool_name == "receive_messages":
        # Agent-runtime-only tool (server.agents.tools.get_agent_runtime_tools)
        # — never advertised outside an agent's own loop, so _nesting is
        # always set here in practice; degrade gracefully if it somehow isn't.
        if _nesting is None:
            return (
                "Error: receive_messages is only available inside an agent's own turn.",
                side_effects,
            )
        from server.agents.messaging import message_bus

        msgs = message_bus.receive(_nesting["agent_id"])
        if not msgs:
            return "No pending messages.", side_effects
        return "\n".join(f"[From {m.from_id}]: {m.content}" for m in msgs), side_effects

    if tool_name == "complete_coordination":
        # Agent-runtime-only tool. The pre-migration inline loop returned
        # immediately on seeing this call (a hard turn-stop); routed through
        # the shared executor there is no such mid-round short-circuit, so
        # it resolves as a normal tool_result carrying the summary — the
        # coordinator's next round then naturally ends the turn once it has
        # nothing further to do (one extra round versus the old immediate
        # stop, not a behavior loss).
        return tool_input.get("summary", "Coordination complete."), side_effects

    if tool_name == "list_agents":
        from server.agent_tools import execute_list_agents

        output = execute_list_agents(session_id)
        return output, side_effects

    if tool_name == "list_sessions":
        from server.agent_tools import execute_list_sessions

        output = execute_list_sessions(session_id)
        return output, side_effects

    if tool_name == "send_session_message":
        from server.agent_tools import execute_send_session_message

        output = execute_send_session_message(tool_input, session_id)
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

    if tool_name == "promote_agent_type":
        from server.agent_tools import execute_promote_agent_type

        output = await _submit(
            loop, executor, lambda ci=tool_input: execute_promote_agent_type(ci, session_id)
        )
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
