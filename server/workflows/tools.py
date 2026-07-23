"""Model-facing workflow tools (ultracode only).

- workflow_run: launch a run. A NEW inline script (or an untrusted saved one)
  returns a preview side-effect for the user to approve before it executes; a
  TRUSTED saved workflow or a resume launches immediately.
- workflow_status: phase tree + journal tail + spend for a run (or the list).
- workflow_save: persist a named workflow (untrusted until approved).
- workflow_list: saved workflows + this session's recent runs.

Dispatched directly in tool_router (like spawn_agent) so they run async on the
server loop with the session's model_id — no worker-thread bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from server.workflows import manager, store
from server.workflows.journal import run_dir
from server.workflows.runtime import parse_workflow

log = logging.getLogger("whisper-studio")

WORKFLOW_TOOLS: list[dict] = [
    {
        "name": "workflow_run",
        "description": (
            "Launch a deterministic multi-agent workflow you author as a JS orchestration "
            "script (see the ultracode directive for the script contract). A new script is "
            "shown to the user for approval before it runs; a trusted saved workflow (by "
            "`name`) or a `resume_from_run_id` launches immediately. Runs detached from this "
            "turn — poll workflow_status for progress. Use for comprehensive, parallel, or "
            "adversarially-verified work; answer directly for simple tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "The workflow JS script (starts with `export const meta = {...}`).",
                },
                "name": {
                    "type": "string",
                    "description": "Run a saved workflow by name instead of an inline script.",
                },
                "args": {"description": "JSON value passed to the script as the global `args`."},
                "budget_usd": {
                    "type": "number",
                    "description": "Optional hard USD cap for the run's agent spend.",
                },
                "resume_from_run_id": {
                    "type": "string",
                    "description": "Resume a failed/stopped run, replaying cached agent results.",
                },
            },
        },
    },
    {
        "name": "workflow_status",
        "description": "Get a workflow run's status, phase/agent journal tail, and spend. Omit run_id to list this session's runs.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
        },
    },
    {
        "name": "workflow_save",
        "description": "Save a named, reusable workflow script (invocable later by name via workflow_run). Stored untrusted until the user approves it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "kebab/snake slug."},
                "script": {"type": "string"},
            },
            "required": ["name", "script"],
        },
    },
    {
        "name": "workflow_list",
        "description": "List saved workflows (name + description + trust) and this session's recent runs.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

WORKFLOW_TOOL_NAMES = {t["name"] for t in WORKFLOW_TOOLS}


def _model_key_for(model_id: str) -> str:
    try:
        from server.infrastructure.config import load_config

        for k, v in (load_config().get("chat_models", {}) or {}).items():
            if v == model_id:
                return k
    except Exception:
        pass
    return "sonnet"


def _preview(
    script: str, meta: dict, budget_usd, *, name: str | None, args, model_id: str = ""
) -> tuple[str, list]:
    phases = meta.get("phases", [])
    side = {
        "workflow_preview": {
            "script": script,
            "name": name or meta.get("name", ""),
            "description": meta.get("description", ""),
            "phases": phases,
            "budget_usd": budget_usd,
            "args": args,
            # Carry the session's model so the approval launch uses it (not the
            # config default).
            "model_id": model_id,
        }
    }
    msg = (
        f"Workflow '{meta.get('name', name or 'workflow')}' is ready with "
        f"{len(phases)} phase(s). Shown to the user for approval — it will run once they "
        "approve. Do not wait; continue or end the turn."
    )
    return msg, [side]


async def execute_workflow_run(tool_input, session_id, model_id, effort_label) -> tuple[str, list]:
    script = (tool_input.get("script") or "").strip()
    name = (tool_input.get("name") or "").strip()
    args = tool_input.get("args")
    budget_usd = tool_input.get("budget_usd")
    resume_from = (tool_input.get("resume_from_run_id") or "").strip()
    model_key = _model_key_for(model_id)

    def _launch(src, *, wf_name, phases, resume=""):
        rid = manager.start_run(
            src,
            args=args,
            session_id=session_id,
            model_key=model_key,
            model_id=model_id,
            effort_label=effort_label,
            budget_usd=budget_usd,
            phases=phases,
            name=wf_name,
            resume_from=resume,
        )
        return json.dumps({"run_id": rid, "status": "running", "name": wf_name}), [
            {"workflow_started": {"run_id": rid, "name": wf_name, "resumed_from": resume}}
        ]

    if resume_from:
        prior = manager.get_run(resume_from)
        if not prior:
            return f"No workflow run '{resume_from}' to resume.", []
        snap = os.path.join(run_dir(resume_from), "workflow.mjs")
        try:
            with open(snap, encoding="utf-8") as f:
                src = f.read()
        except OSError:
            return f"Run '{resume_from}' has no script snapshot to resume.", []
        # A resume replays the SAME deterministic run — reuse the ORIGINAL run's
        # args (not this call's, which is usually omitted); the cache only hits
        # when the re-issued (prompt, opts) match, and those depend on args.
        if args is None:
            args = prior.get("args")
        return _launch(
            src, wf_name=prior.get("name", ""), phases=prior.get("phases", []), resume=resume_from
        )

    if name and not script:
        loaded = store.load_script(name)
        if not loaded:
            return f"No saved workflow named '{name}'.", []
        meta = {"name": name, **(loaded["meta"] or {})}
        if not loaded["trusted"]:
            return _preview(
                loaded["script"], meta, budget_usd, name=name, args=args, model_id=model_id
            )
        return _launch(loaded["script"], wf_name=name, phases=meta.get("phases", []))

    if not script:
        return "Error: provide a `script`, a saved `name`, or a `resume_from_run_id`.", []
    try:
        meta = await asyncio.to_thread(parse_workflow, script)  # spawns node; keep off the loop
    except ValueError as e:
        return f"Workflow script error: {e}", []
    return _preview(script, meta, budget_usd, name=None, args=args, model_id=model_id)


def execute_workflow_status(tool_input, session_id) -> str:
    run_id = (tool_input.get("run_id") or "").strip()
    if run_id:
        run = manager.get_run(run_id)
        if not run:
            return f"No workflow run '{run_id}'."
        return json.dumps(_status_view(run), indent=2)
    runs = manager.list_runs(session_id, limit=15)
    if not runs:
        return "No workflow runs for this session yet."
    return json.dumps({"runs": [_brief(r) for r in runs]}, indent=2)


def _brief(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "name": run.get("name", ""),
        "status": run.get("status"),
        "agents_spawned": run.get("agents_spawned", 0),
        "cost_usd": run.get("cost_usd", 0),
    }


def _status_view(run: dict) -> dict:
    tail = run.get("journal", [])[-30:]
    return {
        **_brief(run),
        "result": run.get("result"),
        "error": run.get("error", ""),
        "journal_tail": tail,
    }


async def execute_workflow_save(tool_input) -> str:
    name = (tool_input.get("name") or "").strip()
    script = (tool_input.get("script") or "").strip()
    if not store.valid_name(name):
        return "Error: name must be a kebab/snake slug (a-z, 0-9, - or _)."
    if not script:
        return "Error: script is required."
    try:
        meta = await asyncio.to_thread(parse_workflow, script)  # spawns node; keep off the loop
    except ValueError as e:
        return f"Workflow script error: {e}"
    store.save_script(name, script, meta, trusted=False)
    return f"Saved workflow '{name}' ({len(meta.get('phases', []))} phases). It is untrusted until you approve it in the Workflows panel; running it will prompt for approval."


def execute_workflow_list(tool_input, session_id) -> str:
    saved = store.list_scripts()
    runs = [_brief(r) for r in manager.list_runs(session_id, limit=10)]
    return json.dumps({"saved_workflows": saved, "recent_runs": runs}, indent=2)
