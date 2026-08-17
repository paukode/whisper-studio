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
from server.workflows.launch_policy import is_session_granted, launch_decision
from server.workflows.runtime import DEFAULT_WORKFLOW_BUDGET_TOKENS, parse_workflow

log = logging.getLogger("whisper-studio")

WORKFLOW_TOOLS: list[dict] = [
    {
        "name": "workflow_run",
        "description": (
            "Launch a deterministic multi-agent workflow you author as a JS orchestration "
            "script (see the ultracode directive for the script contract). A new script is "
            "shown to the user for approval before it runs (or auto-launches when the "
            "user's workflow permission mode allows it within budget); a trusted saved "
            "workflow (by `name`) or a `resume_from_run_id` launches immediately. Runs "
            "detached from this turn — poll workflow_status for progress. Use for "
            "comprehensive, parallel, or adversarially-verified work; answer directly for "
            "simple tasks."
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
                "budget_tokens": {
                    "type": "integer",
                    "description": (
                        "Hard cap on the run's total agent OUTPUT tokens "
                        "(default 600000 when omitted)."
                    ),
                },
                "resume_from_run_id": {
                    "type": "string",
                    "description": "Resume a failed/stopped run, replaying cached agent results.",
                },
                "workspace_path": {
                    "type": "string",
                    "description": (
                        "Pin every agent this run spawns to this folder (e.g. the exact path "
                        "the user named, like '~/Downloads/project1'), instead of whatever "
                        "workspace happens to be connected. Must already exist as a directory — "
                        "use ws_open_folder first if it needs creating. Omit to pin to the "
                        "CURRENTLY connected workspace at launch time (frozen for the whole run, "
                        "so a later workspace switch cannot redirect it mid-run)."
                    ),
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
        "description": "Save a named, reusable workflow script (invocable later by name via workflow_run). A script the user already approved this session is saved trusted; otherwise the first run by name shows an approval preview card, and the user approving it trusts it durably (trust also allows nested workflow(name) calls).",
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
    {
        "name": "workflow_delete",
        "description": "Delete a saved workflow from the library (past runs are kept). The only removal surface — there is no Settings UI for workflows.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The saved workflow's name."}},
            "required": ["name"],
        },
    },
]


def _model_key_for(model_id: str) -> str:
    """Reverse a Bedrock id back to its config key, or "" when nothing matches.

    Returning a plausible-looking "sonnet" on a miss used to be harmless — the
    key only labelled the run row, and the run still executed on the id it was
    given. It is not harmless now that the key also resolves the run's effort: a
    model that exists only in a workspace's .whisper/settings.json misses this
    global lookup, and the run would silently drop from ultracode to Sonnet's
    max. Callers must supply their own default (see _default_model_key).
    """
    try:
        from server.infrastructure.config import load_config

        for k, v in (load_config().get("chat_models", {}) or {}).items():
            if v == model_id:
                return k
    except Exception:
        pass
    return ""


def _default_model_key() -> str:
    """The configured default chat model key — the right fallback when an id
    cannot be reversed. Never "" (an empty key infers the standard ladder, which
    would clamp ultracode away just as silently as the old "sonnet" did)."""
    try:
        from server.infrastructure.config import load_config

        cfg = load_config()
        key = cfg.get("default_chat_model")
        if key and key in (cfg.get("chat_models") or {}):
            return key
        return next(iter(cfg.get("chat_models") or {}), "sonnet")
    except Exception:
        return "sonnet"


def _preview(
    script: str,
    meta: dict,
    budget_tokens,
    *,
    name: str | None,
    args,
    model_id: str = "",
    effort_label: str | None = None,
    workspace_path: str | None = None,
) -> tuple[str, list]:
    phases = meta.get("phases", [])
    side = {
        "workflow_preview": {
            "script": script,
            "name": name or meta.get("name", ""),
            "description": meta.get("description", ""),
            "phases": phases,
            "budget_tokens": budget_tokens,
            "args": args,
            # Carry the session's model AND effort so the approval launch runs
            # the run identically to an auto-approved one. Without the effort
            # the card's launch fell back to the provider default, so clicking
            # Approve silently downgraded every child agent's reasoning.
            "model_id": model_id,
            "effort_label": effort_label or "",
            # The resolved root this run will be pinned to — shown so the user
            # approves the actual folder the agents will touch, not a guess.
            "workspace_path": workspace_path or "",
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
    budget_tokens = tool_input.get("budget_tokens")
    resume_from = (tool_input.get("resume_from_run_id") or "").strip()
    model_key = _model_key_for(model_id) or _default_model_key()

    def _launch(
        src, *, wf_name, phases, resume="", auto=False, session_grant=False, workspace_path=None
    ):
        rid = manager.start_run(
            src,
            args=args,
            session_id=session_id,
            model_key=model_key,
            model_id=model_id,
            effort_label=effort_label,
            workspace_path=workspace_path,
            budget_tokens=budget_tokens,
            phases=phases,
            name=wf_name,
            resume_from=resume,
        )
        body = {"run_id": rid, "status": "running", "name": wf_name}
        if auto:
            # Launched with no card because the user's "workflow" category
            # mode auto-approves within the token threshold.
            body["auto_approved"] = True
        if session_grant:
            # Launched with no card because the user approved this exact
            # script from a preview card earlier in this session.
            body["session_approved"] = True
        return json.dumps(body), [
            {"workflow_started": {"run_id": rid, "name": wf_name, "resumed_from": resume}}
        ]

    def _denied() -> tuple[str, list]:
        return (
            "Workflow launches are disabled by the current permission settings "
            "(workflow category mode dontAsk). Not started. The user can change "
            "this in Settings > Permissions.",
            [],
        )

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
        # Same for the budget: cached replays cost zero tokens, so the original
        # cap is the right default for the remainder of the run.
        if budget_tokens is None:
            budget_tokens = prior.get("budget_tokens")
        if budget_tokens is None:
            budget_tokens = DEFAULT_WORKFLOW_BUDGET_TOKENS
        # And the effort: a resume continues the ORIGINAL run, so the agents it
        # still has to execute reason at the level the run was approved at, not
        # at whatever the composer happens to be set to now.
        effort_label = prior.get("effort_label") or effort_label
        # Same for the workspace: a resume replays the SAME deterministic run
        # against the folder it was originally pinned to, never re-resolved
        # from whatever happens to be connected now.
        workspace_path = prior.get("workspace_path")
        return _launch(
            src,
            wf_name=prior.get("name", ""),
            phases=prior.get("phases", []),
            resume=resume_from,
            workspace_path=workspace_path,
        )

    # Resolve the default before preview/launch so the approval card and the
    # run row always carry the real, enforced number.
    if budget_tokens is None:
        budget_tokens = DEFAULT_WORKFLOW_BUDGET_TOKENS

    # The root every agent in this run resolves against, frozen NOW — before
    # any approval delay, and before the workflow can outlive a later
    # workspace switch. An explicit path must already exist; see
    # resolve_workflow_workspace's docstring for why this never falls back or
    # creates one.
    workspace_path, ws_error = manager.resolve_workflow_workspace(tool_input.get("workspace_path"))
    if ws_error:
        return ws_error, []

    if name and not script:
        loaded = store.load_script(name)
        if not loaded:
            return f"No saved workflow named '{name}'.", []
        meta = {"name": name, **(loaded["meta"] or {})}
        if not loaded["trusted"]:
            # Approving the preview card once covers the rest of the session:
            # the grant is keyed by script hash, so it holds exactly while the
            # saved script is byte-identical to what the user approved.
            if is_session_granted(session_id, loaded["script"]):
                return _launch(
                    loaded["script"],
                    wf_name=name,
                    phases=meta.get("phases", []),
                    session_grant=True,
                    workspace_path=workspace_path,
                )
            decision = launch_decision(budget_tokens)
            if decision == "deny":
                return _denied()
            if decision == "auto":
                return _launch(
                    loaded["script"],
                    wf_name=name,
                    phases=meta.get("phases", []),
                    auto=True,
                    workspace_path=workspace_path,
                )
            return _preview(
                loaded["script"],
                meta,
                budget_tokens,
                name=name,
                args=args,
                model_id=model_id,
                effort_label=effort_label,
                workspace_path=workspace_path,
            )
        return _launch(
            loaded["script"],
            wf_name=name,
            phases=meta.get("phases", []),
            workspace_path=workspace_path,
        )

    if not script:
        return "Error: provide a `script`, a saved `name`, or a `resume_from_run_id`.", []
    try:
        meta = await asyncio.to_thread(parse_workflow, script)  # spawns node; keep off the loop
    except ValueError as e:
        return f"Workflow script error: {e}", []
    # The same inline script approved from a card earlier this session (byte
    # identical, per hash) launches without a second card.
    if is_session_granted(session_id, script):
        return _launch(
            script,
            wf_name=meta.get("name", ""),
            phases=meta.get("phases", []),
            session_grant=True,
            workspace_path=workspace_path,
        )
    decision = launch_decision(budget_tokens)
    if decision == "deny":
        return _denied()
    if decision == "auto":
        return _launch(
            script,
            wf_name=meta.get("name", ""),
            phases=meta.get("phases", []),
            auto=True,
            workspace_path=workspace_path,
        )
    return _preview(
        script,
        meta,
        budget_tokens,
        name=None,
        args=args,
        model_id=model_id,
        effort_label=effort_label,
        workspace_path=workspace_path,
    )


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


async def execute_workflow_save(tool_input, session_id: str = "") -> str:
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
    # A script the user already approved this session is saved TRUSTED: the
    # approval was for these exact bytes, so re-asking after a save would be
    # the second ask the card-once rule exists to prevent.
    trusted = is_session_granted(session_id, script)
    store.save_script(name, script, meta, trusted=trusted)
    if trusted:
        return (
            f"Saved workflow '{name}' ({len(meta.get('phases', []))} phases) as trusted — "
            "the user approved this script, so runs by name start immediately."
        )
    return (
        f"Saved workflow '{name}' ({len(meta.get('phases', []))} phases). The first run by "
        "name shows the user an approval preview card; approving it trusts the workflow "
        "from then on."
    )


def execute_workflow_delete(tool_input) -> str:
    name = (tool_input.get("name") or "").strip()
    if not store.valid_name(name):
        return "Error: name must be a kebab/snake slug (a-z, 0-9, - or _)."
    if not store.delete_script(name):
        return f"No saved workflow named '{name}'."
    return f"Deleted workflow '{name}'. Past runs and their journals are kept."


def execute_workflow_list(tool_input, session_id) -> str:
    saved = store.list_scripts()
    runs = [_brief(r) for r in manager.list_runs(session_id, limit=10)]
    return json.dumps({"saved_workflows": saved, "recent_runs": runs}, indent=2)
