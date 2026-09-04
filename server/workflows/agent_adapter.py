"""One ``agent()`` RPC → one WS-C ``run_agent`` call.

Maps the harness opts (label/phase/schema/effort/isolation/agentType/model) to
run_agent kwargs and normalizes the AgentResult back to the JS-facing shape
``{text, output, usage, status, agent_id}``. Structured-output validation +
one repair retry already live inside run_agent (WS-C), so passing
``structured_schema`` is all that's needed — this stays a thin seam.
"""

from __future__ import annotations

import logging

log = logging.getLogger("whisper-studio")


def _agent_effort(opts: dict, run_effort: str | None) -> str | None:
    """The effort for ONE workflow agent: the script's `effort` opt when it gave
    one, else the run's own level.

    The script contract documents Claude Code's raw names ("xhigh"), while the
    app speaks labels ("extra"), so a script asking for xhigh used to fall
    through every mapping and land on high at Anthropic but medium at OpenAI —
    the same word, two different depths. normalize_effort folds the raw names
    onto app labels so one vocabulary reaches both providers; run_agent then
    clamps whatever comes out to the child model's own ladder.

    "ultracode" is deliberately NOT accepted from a script: it is the mode that
    launched this run, and a child agent inside a workflow does not orchestrate
    a second one. A script asking for it gets the run's level, which already
    carries ultracode's reasoning depth.
    """
    from server.infrastructure.effort import known_effort_label

    label = known_effort_label(opts.get("effort"))
    if label is None or label == "ultracode":
        # Absent, misspelt, or ultracode — inherit rather than invent a level.
        return run_effort
    return label


def _resolve_model(opts: dict, default_model_id: str) -> tuple[str | None, str, str]:
    """Resolve a script's ``opts.model`` (a config KEY, e.g. 'sonnet') to a
    Bedrock id. Returns ``(model_id, effective_key, warning)``.

    The validation itself (unknown key falls back with a warning, on-device
    keys refused up front) is shared with the spawn_agent tool — see
    server/agents/model_resolve.py for the failure-mode rationale.
    """
    from server.agents.model_resolve import resolve_model_override

    return resolve_model_override(opts.get("model"), default_model_id)


async def run_workflow_agent(
    prompt: str,
    opts: dict,
    *,
    session_id: str,
    default_model_id: str,
    effort_label: str | None,
    workspace_path: str | None,
    run_id: str,
    depth: int,
) -> dict:
    """Execute one workflow agent. Never raises for an agent-level failure —
    returns ``status:"failed"`` so the script decides (the harness contract)."""
    from server.agents.runtime import run_agent

    opts = opts or {}
    agent_type = opts.get("agentType") or "general"
    schema = opts.get("schema") if isinstance(opts.get("schema"), dict) else None
    effort = _agent_effort(opts, effort_label)
    isolation = "worktree" if opts.get("isolation") == "worktree" else "none"
    model_id, effective_model_key, model_warning = _resolve_model(opts, default_model_id)
    if model_warning:
        log.warning("workflow %s agent model override: %s", run_id, model_warning)

    try:
        res = await run_agent(
            prompt,
            agent_type=agent_type,
            # The script's label (e.g. "review:bugs") rides every progress event
            # as agent_name so the per-agent card shows a meaningful title
            # instead of a bare agent id.
            agent_name=(opts.get("label") or None),
            session_id=session_id,
            model_id_override=model_id,
            effort_label=effort,
            # The run's pinned root, not whatever happens to be globally
            # connected when this particular agent starts — a workflow can
            # run long enough for the user to switch workspaces mid-run.
            workspace_path=workspace_path,
            structured_schema=schema,
            isolation=isolation,
            depth=depth,
            event_channel=f"workflow:{run_id}",
        )
    except Exception as e:  # noqa: BLE001 — surface as a failed agent, never crash the run
        log.warning("workflow agent error (run %s): %s", run_id, e)
        return {
            "text": f"[agent error] {e}",
            "output": None,
            "usage": {},
            "status": "failed",
            "agent_id": "",
        }

    return {
        # The key the work ACTUALLY ran on, so the ledger prices reality
        # rather than an override that silently fell back (see _resolve_model).
        # Empty means "the run's own model" — the caller's default.
        "model_key": effective_model_key,
        "model_warning": model_warning,
        "text": res.output or "",
        "output": res.structured_output,
        "usage": res.usage or {},
        "status": res.status or "completed",
        "agent_id": res.agent_id or "",
    }
