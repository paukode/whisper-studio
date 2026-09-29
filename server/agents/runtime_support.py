"""Helpers for the agent runtime that need no access to the run loop:
worktree isolation and harvest, and the structured-output distillation.
Split out of runtime.py to keep that module inside the file-size budget."""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from server.agents.config import AgentConfig  # noqa: E402

if TYPE_CHECKING:
    from server.agents.runtime import AgentResult

log = logging.getLogger("whisper-studio")

_git_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-git")

# Ambient nesting context for a spawn_agent/send_message/receive_messages/


def _enter_agent_worktree(agent_id: str, session_id: str, repo_root: str | None = None):
    """Create (or resume) an isolated git worktree for this agent.

    Only when the target is a git repo; failures degrade to the shared
    workspace with a warning (isolation is an optimization for parallel
    writes, not a correctness gate). Returns the WorktreeSession so run_agent
    can HARVEST it on completion: the agent's changes are applied uncommitted
    back to the originating working tree and the worktree + branch are
    removed (see server/git/worktree_harvest.py).

    ``repo_root`` lets a caller with its own pinned root (a workflow run) fork
    from that instead of whatever is globally connected. This runs on a
    worker thread via loop.run_in_executor, which does NOT propagate
    contextvars, so a caller cannot rely on set_workspace_override being
    visible here — it must pass the root explicitly.
    """
    try:
        import os as _os

        from server.git.worktree_session import enter_worktree
        from server.workspace.state import get_workspace_path

        repo_root = repo_root or get_workspace_path()
        if not repo_root or not _os.path.exists(_os.path.join(repo_root, ".git")):
            return None
        # Namespaced session key: enter_worktree records a session->worktree
        # mapping meant for CHAT sessions; an agent must never clobber the
        # user's own worktree state for the real session id.
        return enter_worktree(repo_root, f"agent-{agent_id}", f"agent:{agent_id}")
    except Exception as e:
        log.warning("agent worktree isolation failed (%s); using shared workspace", e)
        return None


def _spawned_by_released_turn(workspace_path: str | None) -> bool:
    """True when an agent with no pinned root is spawned by a turn whose
    workspace the user let go of (a disconnect, or a switch in the UI).

    Such an agent inherits that turn's latch, so its workspace tools are
    refused. It must not fork a worktree either: the fork runs on a worker
    thread that cannot see the latch, so it would fork, and later harvest
    into, whatever folder the user connected in its place."""
    if workspace_path:
        return False
    from server.workspace.state import workspace_lost_message

    return workspace_lost_message() is not None


def _agent_finished(result: AgentResult) -> bool:
    """True only when the agent genuinely completed its goal — so its worktree
    work is applied. A turn/deadline limit reports status='completed' (kept that
    way for memory/subagent callers) but sets stopped_early, meaning the work is
    partial and the worktree is kept for inspection instead of applied."""
    return result.status == "completed" and not result.stopped_early


async def _harvest_worktree(wt_session, agent_id: str, apply_changes: bool) -> str:
    """Run the worktree harvest off-loop (dedicated git pool) and return its
    user-facing note."""
    from functools import partial

    from server.git.worktree_harvest import harvest_agent_worktree

    call = partial(
        harvest_agent_worktree,
        wt_session.original_cwd,
        wt_session.worktree_path,
        wt_session.worktree_branch,
        f"agent:{agent_id}",
        apply_changes,
        base_commit=wt_session.original_head_commit,
    )
    outcome = await asyncio.get_running_loop().run_in_executor(_git_executor, call)
    return outcome.get("note", "")


def _record_distill_call(counts: dict, *, session_id: str, model_key: str, source: str) -> None:
    """One structured-output call in the cost log: the only agent spend the
    turn engine never sees, so it is recorded here, once per attempt, with
    the counts server.costs.capture took from its payload (or estimated as
    characters / 4, and marked so)."""
    try:
        from server.costs.calls import record_counts

        record_counts(
            counts, model_key=model_key or "unknown", source=source, session_id=session_id
        )
    except Exception as e:  # noqa: BLE001 - a cost write never breaks the run
        log.error("Cost log: could not record a structured-output call: %s", e)


async def _distill_structured(
    adapter, system, messages, schema, config, total_usage, *, session_id, model_key, source
):
    """One structured-output call over the finished transcript, with a single
    repair retry, each attempt recorded in the cost log under the run's
    ``source`` by the adapter's counts hook (so an attempt that breaks after
    the request was accepted is recorded too).

    Nothing forces the emit_result call (several models reject a forced
    tool_choice), so the ask names the tool, and the retry says what went
    wrong: the object did not validate, or the reply was text instead of the
    result. jsonschema is a hard dependency of the venv (via mcp) but a second
    failure returns None rather than raising; callers decide whether an
    unstructured fallback is acceptable."""
    from functools import partial

    from server.agents.providers.base import TurnUsage as _TU
    from server.costs.sources import require_source

    require_source(source)  # an unlabelled source is a programming error
    record = partial(
        _record_distill_call, session_id=session_id, model_key=model_key, source=source
    )
    ask = {
        "role": "user",
        "content": (
            "Now emit the final structured result for the task above using the "
            "emit_result tool (or the required JSON format). Output the complete "
            "object only."
        ),
    }
    attempt_messages = [*messages, ask]
    for attempt in range(2):
        turn = await adapter.invoke(
            system=system,
            messages=attempt_messages,
            tools=None,
            max_tokens=config.max_tokens,
            effort_label=None,
            force_structured=schema,
            on_counts=record,
        )
        if isinstance(turn.usage, _TU):
            total_usage.add(turn.usage)
        candidate = turn.structured_output
        if candidate is not None:
            try:
                import jsonschema

                jsonschema.validate(candidate, schema)
                return candidate
            except Exception as e:
                if attempt == 0:
                    attempt_messages = [
                        *attempt_messages,
                        {"role": "assistant", "content": json.dumps(candidate)},
                        {
                            "role": "user",
                            "content": f"That did not validate against the schema ({e}). "
                            "Call the emit_result tool again (or answer in the required "
                            "JSON format) with a corrected complete object.",
                        },
                    ]
                    continue
                log.warning("structured output failed validation twice: %s", e)
                return None
        if attempt == 0 and turn.text.strip():
            # Answered in text instead of calling the tool: show the reply
            # back and ask for the call. An empty reply is simply asked again.
            attempt_messages = [
                *attempt_messages,
                {"role": "assistant", "content": turn.text},
                {
                    "role": "user",
                    "content": "That reply was not the result object. Call the "
                    "emit_result tool now (or answer in the required JSON format) "
                    "with the complete object.",
                },
            ]
    return None


async def distill_run_output(
    *,
    model_key: str,
    model_id: str,
    system: str,
    messages: list[dict],
    final_text: str,
    stopped_early: bool,
    schema: dict,
    config,
    usage: dict,
    session_id: str,
    source: str,
) -> tuple[dict | None, dict]:
    """The structured result of a finished agent or headless run, and the
    run's usage with the distillation calls added.

    The calls go through the old provider adapters (server.agents.providers):
    a one-shot structured-output call the chat/engine adapters have no
    equivalent for. A run that stopped on a limit already ends with its last
    tool result; a natural finish gets its final answer appended as one more
    assistant turn first."""
    from server.agents.providers import TurnUsage, get_adapter

    total = TurnUsage(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cache_read_tokens=usage["cache_read_tokens"],
        cache_creation_tokens=usage["cache_creation_tokens"],
        cost_usd=usage["cost_usd"],
    )
    if not stopped_early:
        answer = {
            "role": "assistant",
            "content": [{"type": "text", "text": final_text or "(done)"}],
        }
        messages = [*messages, answer]
    structured = await _distill_structured(
        get_adapter(model_key, model_id),
        system,
        messages,
        schema,
        config,
        total,
        session_id=session_id,
        model_key=model_key,
        source=source,
    )
    return structured, total.as_dict()


def _with_session_id(tool_input: dict, session_id: str) -> dict:
    """Return a COPY of the model's tool input with internal markers injected
    for the executor: the session id, and an unattended-agent stamp.

    The original ``tool_input`` is ``tu["input"]`` — the exact dict that lives
    inside the assistant message replayed to Bedrock on every subsequent turn.
    Mutating it in place would leak the internal ``__session_id__``/``__agent__``
    keys into the transcript (where the model can see and imitate them), and
    some executors only ``.get()`` rather than ``.pop()`` them, so they'd
    persist. Copying keeps ``tu["input"]`` pristine, mirroring the main chat
    path (tool_executor.py).

    ``__agent__`` marks every tool call dispatched from this unattended loop —
    no human is present to answer on-the-spot questions. High-blast-radius
    executors (github mutations via refuse_if_agent, and MCP's elicitation
    callback) check for this stamp and refuse/auto-decline rather than acting
    or answering on a human's behalf.
    """
    call_input = dict(tool_input)
    call_input["__session_id__"] = session_id
    call_input["__agent__"] = True
    return call_input


def _resolve_agent_model(model_id_override: str | None, config: AgentConfig) -> str | None:
    """Resolve the model id for an agent run.

    An explicit override is returned verbatim (the spawn handlers already
    threaded the session-selected model through it). Without one, fall back to
    the user's configured default chat model. An on-device (``local:*``)
    candidate is accepted only when the model supports tool calling (the same
    registry gate interactive chat uses): a chat-only local model cannot
    drive an agent loop, and letting one through is how background memory
    agents broke in hybrid mode. Local mode keeps everything on this Mac, so
    there only an on-device model runs: a cloud override is refused instead of
    being sent to Bedrock, and the fallback skips cloud entries. Returns None
    when nothing can run the agent (``no_agent_model_reason`` says why);
    run_agent fails the run early instead of erroring at the provider call.
    """
    from server.agents.providers import model_key_for_id
    from server.infrastructure.config import load_config
    from server.infrastructure.model_mode import current_mode
    from server.local.runtime import is_local_model_id, supports_tools

    cfg = load_config()
    on_device_only = current_mode(cfg) == "local"
    if model_id_override:
        if on_device_only and not is_local_model_id(model_id_override):
            return None
        return model_id_override

    chat_models = cfg.get("chat_models", {})
    default_key = cfg.get("default_chat_model")
    candidates = [
        chat_models.get(default_key) if default_key else None,
        chat_models.get("sonnet"),
        *chat_models.values(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if on_device_only and not is_local_model_id(candidate):
            continue
        if is_local_model_id(candidate) and not supports_tools(model_key_for_id(candidate)):
            continue
        return candidate
    return None


def no_agent_model_reason(model_id_override: str | None) -> str:
    """Why ``_resolve_agent_model`` returned None, in words the user reads."""
    from server.agents.providers import model_key_for_id
    from server.infrastructure.config import load_config
    from server.infrastructure.model_mode import (
        NO_LOCAL_AGENT_MODEL_REASON,
        current_mode,
        turn_model_refusal,
    )
    from server.local.runtime import is_local_model_id

    cfg = load_config()
    if current_mode(cfg) != "local":
        return (
            "No agent-capable chat model configured: every chat_models entry is an "
            "on-device model without tool support."
        )
    if model_id_override and not is_local_model_id(model_id_override):
        key = model_key_for_id(model_id_override) or model_id_override
        label = ((cfg.get("chat_model_meta") or {}).get(key) or {}).get("label", "")
        refusal = turn_model_refusal(key, on_device=False, label=label, mode="local")
        if refusal:
            return refusal
    return NO_LOCAL_AGENT_MODEL_REASON


def budget_readout(
    config: AgentConfig,
    extension: dict | None,
    *,
    next_turn: int,
    elapsed: float,
    usage: dict,
    cost_capped: bool,
) -> dict:
    """Turns, time and estimated cost so far, plus whether the next round is
    the final one. Stamped on every turn_start event; the card's budget bars
    and state pill read it. The cost is the engine's own (each round priced
    at its tier), never the summed counts priced again. Pure: every input is
    passed in."""
    from server.chat.engine.runner import SOFT_LIMIT_FRACTION

    ext = extension or {}
    cap = config.max_turns + int(ext.get("rounds") or 0)
    deadline_s = (
        float(config.deadline_seconds) + float(ext.get("seconds") or 0.0)
        if config.deadline_seconds is not None
        else None
    )
    finishing = (
        next_turn >= cap
        or (deadline_s is not None and elapsed >= SOFT_LIMIT_FRACTION * deadline_s)
        or cost_capped
        or bool(ext.get("finish"))
    )
    return {
        "max_turns": cap,
        "elapsed_s": round(elapsed, 1),
        "deadline_s": deadline_s,
        "cost_usd": round(float(usage["cost_usd"]), 4),
        "budget_state": "finishing" if finishing else "working",
    }
