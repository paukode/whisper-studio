"""The single hook evaluation pipeline.

One ``run_hooks(event, payload)`` per event, covering every model path because
it is called from the shared tool executor and the shared end-of-turn gates.
Order per event: in-process hooks (plugins, WS-E's orchestrator gate) first,
then user shell hooks, then trusted project shell hooks. First deny wins;
input rewrites and context chain in declaration order.

Shell contract:
- stdin: one JSON payload (schema.build_stdin_payload). Legacy WHISPER_* env
  vars are also set for existing hooks.
- exit 0 = pass; stdout, if JSON, may carry {decision, reason, updatedInput,
  additionalContext}; non-JSON stdout on Post/SessionStart becomes context.
- exit 2 = block; stderr is the model-visible reason.
- other exit / timeout / spawn error = infra error, resolved per-hook by
  on_error ("ignore" default, "block" fail-closed).
Executed via server/sandbox.run_sandboxed (deny-list, process-group kill).
"""

from __future__ import annotations

import asyncio
import json
import logging

from server.hooks.config_loader import merged_for_event
from server.hooks.schema import HookDef, HookOutcome, build_stdin_payload, matches
from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

MAX_STOP_BLOCKS_PER_TURN = 3


def _apply_control(control: dict, outcome: HookOutcome, *, allow_rewrite: bool) -> bool:
    """Fold one structured-control dict into the outcome. Returns True if it
    denied (caller short-circuits)."""
    decision = (control.get("decision") or "").lower()
    if decision == "deny":
        outcome.decision = "deny"
        outcome.reason = control.get("reason") or "Blocked by hook."
        if control.get("_findings") is not None:
            outcome.findings = control["_findings"]
        return True
    if decision == "rewrite" and allow_rewrite:
        upd = control.get("updatedInput")
        if isinstance(upd, dict):
            outcome.updated_input = upd
    ctx = control.get("additionalContext")
    if isinstance(ctx, str) and ctx.strip():
        outcome.contexts.append(ctx.strip())
    return False


def _run_shell_hook(hook: HookDef, payload: dict, cwd: str) -> tuple[int, str, str]:
    from server.sandbox import run_sandboxed

    # Legacy convenience vars (the full payload is also on stdin). Passed via the
    # subprocess environment, NOT a command prefix — a `VAR=val <command>` prefix
    # would turn any command starting with a shell compound construct
    # (if/for/while/case/{}/()) into a /bin/sh syntax error whose exit code (2)
    # is exactly the engine's DENY code, silently blocking every matched call.
    env_extra = {
        "WHISPER_EVENT": payload.get("event", ""),
        "WHISPER_TOOL": payload.get("tool_name", "") or "",
        "WHISPER_INPUT": json.dumps(payload.get("tool_input", {}))[:500],
        "WHISPER_OUTPUT": str(payload.get("tool_output", ""))[:500],
        "WHISPER_SESSION": payload.get("session_id", ""),
    }
    result = run_sandboxed(
        hook.command,
        cwd=cwd,
        timeout=hook.timeout,
        input_data=json.dumps(payload),
        env_extra=env_extra,
    )
    return result.returncode, (result.stdout or ""), (result.stderr or "")


async def run_hooks(
    event: str,
    payload: dict,
    *,
    tool_name: str | None = None,
    workspace: str | None = None,
    allow_rewrite: bool = True,
) -> HookOutcome:
    """Evaluate all hooks for an event. See module docstring for the contract."""
    from server.infrastructure.plugin_hooks import run_inprocess

    outcome = HookOutcome()
    ws = workspace or _current_workspace()
    cwd = ws or data_root()

    # Phase 1: in-process hooks (plugins, orchestrator gate).
    for control in await run_inprocess(event, payload):
        if _apply_control(control, outcome, allow_rewrite=allow_rewrite):
            return outcome

    # Phase 2: shell hooks (user then trusted project), matcher-filtered.
    # A rewrite chains: subsequent hooks see the latest tool_input so the
    # contract's "input rewrites chain in declaration order" actually holds.
    loop = asyncio.get_event_loop()
    for hook in merged_for_event(event, ws):
        if not matches(hook.matcher, tool_name):
            continue
        hook_payload = payload
        if allow_rewrite and outcome.updated_input is not None:
            hook_payload = {**payload, "tool_input": outcome.updated_input}
        try:
            code, stdout, stderr = await loop.run_in_executor(
                None, _run_shell_hook, hook, hook_payload, cwd
            )
        except Exception as e:  # timeout / spawn failure = infra error
            if hook.on_error == "block":
                outcome.decision = "deny"
                outcome.reason = f"Hook '{hook.id}' failed ({e}) and is fail-closed."
                return outcome
            outcome.errors.append(f"hook {hook.id}: {e}")
            continue

        if code == 2:
            outcome.decision = "deny"
            outcome.reason = (stderr or "Blocked by hook.").strip()
            return outcome
        if code != 0:
            if hook.on_error == "block":
                outcome.decision = "deny"
                outcome.reason = (stderr or f"Hook exited {code}").strip()
                return outcome
            outcome.errors.append(f"hook {hook.id}: exit {code}: {stderr.strip()[:200]}")
            continue

        # exit 0: parse stdout for structured control, else treat as context.
        stripped = stdout.strip()
        if stripped:
            control = _try_json(stripped)
            if control is not None:
                if _apply_control(control, outcome, allow_rewrite=allow_rewrite):
                    return outcome
            elif event in ("PostToolUse", "SessionStart", "UserPromptSubmit"):
                outcome.contexts.append(stripped)
    return outcome


def _try_json(text: str) -> dict | None:
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else None
    except (ValueError, TypeError):
        return None


def _current_workspace() -> str | None:
    try:
        from server.workspace import get_workspace_path

        return get_workspace_path()
    except Exception:
        return None


async def check_stop_hooks(
    session_id: str,
    workspace: str | None,
    *,
    stop_hook_active: bool = False,
    model_id: str = "",
) -> HookOutcome:
    """The WS-E-critical gate: run Stop hooks at end of turn. A deny means the
    turn must not finish — the caller injects the reason and continues the
    loop (bounded by MAX_STOP_BLOCKS_PER_TURN and the model's round cap)."""
    payload = build_stdin_payload(
        "Stop",
        session_id=session_id,
        workspace=workspace or "",
        stop_hook_active=stop_hook_active,
        model_id=model_id,
    )
    return await run_hooks(
        "Stop", payload, tool_name=None, workspace=workspace, allow_rewrite=False
    )


def dry_run(
    command: str, sample_payload: dict, *, timeout: int = 10, cwd: str | None = None
) -> dict:
    """Execute a hook command against a sample payload for the Test button."""
    from server.sandbox import run_sandboxed

    try:
        result = run_sandboxed(
            command,
            cwd=cwd or data_root(),
            timeout=max(1, min(int(timeout), 60)),
            input_data=json.dumps(sample_payload),
        )
        return {
            "exit_code": result.returncode,
            "stdout": (result.stdout or "")[:4000],
            "stderr": (result.stderr or "")[:4000],
        }
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": f"{e}"}
