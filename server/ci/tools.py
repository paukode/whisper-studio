"""Model-facing CI tools (ultracode only).

- ci_watch:   start watching a branch's latest run detached; returns a task id.
- ci_status:  the latest run + failed jobs for a branch (one-shot).
- ci_autofix: diagnose a failing run and hand back an APPROVABLE WS-D workflow
              (via the shared workflow_preview card) that fixes then verifies.

Dispatched directly in tool_router so they run async on the server loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from server.ci import autofix, manager, provider

log = logging.getLogger("whisper-studio")

CI_TOOLS: list[dict] = [
    {
        "name": "ci_watch",
        "description": (
            "Watch a branch's latest GitHub Actions run to its conclusion, detached from "
            "this turn — a live card updates as jobs finish and a result card lands when it "
            "settles. Defaults to the current branch. Use after pushing to see if CI passes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch to watch (default: current)."}
            },
        },
    },
    {
        "name": "ci_status",
        "description": "Get the latest CI run and any failed jobs for a branch (default: current). One-shot, no watching.",
        "input_schema": {
            "type": "object",
            "properties": {"branch": {"type": "string"}},
        },
    },
    {
        "name": "ci_autofix",
        "description": (
            "Diagnose a branch's failing CI and propose a fix as an approvable workflow: it "
            "reads the failing logs, identifies each failure, and generates a workflow that "
            "applies the minimal fixes then runs the verification gate. The workflow is shown "
            "for approval before it edits anything. Default branch: current."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"branch": {"type": "string"}},
        },
    },
]

CI_TOOL_NAMES = {t["name"] for t in CI_TOOLS}


def _resolve(tool_input: dict) -> tuple[str, str]:
    """(branch, cwd) — branch defaults to the workspace's current branch."""
    cwd = (tool_input.get("cwd") or "").strip()
    if not cwd:
        try:
            from server.workspace.state import get_workspace_path

            cwd = get_workspace_path() or os.getcwd()
        except Exception:
            cwd = os.getcwd()
    branch = (tool_input.get("branch") or "").strip()
    if not branch:
        try:
            from server.git.core import get_branch

            branch = get_branch(cwd) or "HEAD"
        except Exception:
            branch = "HEAD"
    return branch, cwd


def _no_gh() -> tuple[str, list]:
    return (
        "The GitHub CLI (`gh`) is not installed or not on PATH, so CI can't be queried. "
        "Install it (https://cli.github.com) and run `gh auth login`.",
        [],
    )


async def execute_ci_watch(tool_input, session_id) -> tuple[str, list]:
    if not provider.gh_available():
        return _no_gh()
    branch, cwd = _resolve(tool_input)
    task_id = manager.start_watch(branch, cwd, session_id)
    return (
        json.dumps({"task_id": task_id, "branch": branch, "status": "watching"}),
        [{"ci_started": {"task_id": task_id, "branch": branch}}],
    )


async def execute_ci_status(tool_input, session_id) -> str:
    if not provider.gh_available():
        return _no_gh()[0]
    branch, cwd = _resolve(tool_input)
    snap = await asyncio.to_thread(manager.status_snapshot, branch, cwd)
    return json.dumps(snap, indent=2)


async def execute_ci_autofix(tool_input, session_id, model_id) -> tuple[str, list]:
    if not provider.gh_available():
        return _no_gh()
    branch, cwd = _resolve(tool_input)
    run = await asyncio.to_thread(provider.latest_run, branch, cwd)
    if not run:
        return f"No CI runs found for branch '{branch}'.", []
    if not provider.is_terminal(run):
        return (
            f"The latest run for '{branch}' is still in progress — watch it, then autofix if it fails.",
            [],
        )
    if not provider.is_failing(run):
        return (
            f"The latest run for '{branch}' ({run.get('conclusion')}) isn't failing — nothing to autofix.",
            [],
        )

    full = await asyncio.to_thread(provider.get_run, run["run_id"], cwd)
    plan = await asyncio.to_thread(autofix.plan_autofix, full or run, cwd, session_id=session_id)
    if not plan.get("script"):
        return plan.get("summary", "No actionable failure found."), []

    # Reuse WS-D's approval preview: the generated fix workflow runs only after
    # the user approves it, with D's budget/journal/run card.
    preview = {
        "workflow_preview": {
            "script": plan["script"],
            "name": "ci-autofix",
            "description": plan["summary"],
            "phases": [{"title": "Fix"}, {"title": "Verify"}],
            "budget_usd": plan.get("budget_usd"),
            "args": None,
            "model_id": model_id,
        }
    }
    diagnosis = {
        "ci_diagnosis": {
            "branch": branch,
            "run_id": plan["run_id"],
            "url": plan.get("url"),
            "findings": plan["findings"],
        }
    }
    return (
        f"CI autofix plan for '{branch}': {plan['summary']}. A fix workflow is shown for approval; "
        "it will edit files and verify only once approved. Do not wait; continue or end the turn.",
        [diagnosis, preview],
    )
