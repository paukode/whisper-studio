"""The voice tool set Nova Sonic gets.

Deliberately tiny. Sonic is the voice, not the brain: anything that needs real
work goes through ``ask_assistant``, which runs one normal chat turn
(server.exec.headless.run_headless_turn, ``attended=True``) with the full tool
pool on the user's selected cloud model, so Claude keeps doing the thinking and
the existing tool catalog, workspace and permission policy apply unchanged.

Attended means the human is present but not at the chat UI: whatever would
show an approval card or a question in chat pauses the delegated turn instead,
``ask_assistant`` reports it, Sonic asks the user out loud, and
``resolve_request`` feeds the decision back and resumes the very same turn.
That is how a spoken "yes" opens a folder, runs a command or answers a question
with exactly the checks typed chat has.

Long work runs in the background. ``ask_assistant`` waits ASK_WAIT_S for a quick
answer; past that it returns "Working on it" and the run keeps going, other
requests are accepted meanwhile, and the result is delivered when it lands: to
the browser as an ``assistant_answer`` event (the written bubble) and to Sonic
as a typed turn it then summarizes. Nothing is discarded because it took long.

Tool schemas are the app's usual Anthropic shape ``{name, description,
input_schema}``; protocol.to_tool_spec converts them for Sonic.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]

# The UI gets the full written answer; the text handed to Sonic is trimmed in
# session.py.
MAX_RESULT_CHARS = 12000
_PREVIEW_CHARS = 160
_LATE_NOTE_CHARS = 2500
# Rounds one delegated turn may take (agents and multi-step work need room).
ASSISTANT_MAX_ROUNDS = 40
# How long ask_assistant waits before letting the run continue in the background.
ASK_WAIT_S = 20.0
# Safety cap for a background run. On expiry whatever it produced is delivered
# with a note; work is never silently discarded.
ASSISTANT_DEADLINE_S = 1800.0
MAX_CONCURRENT_RUNS = 3
WORKING_PREFIX = "Working on it:"

_APPROVE_WORDS = (
    "approve",
    "yes",
    "yeah",
    "yep",
    "ok",
    "okay",
    "sure",
    "go ahead",
    "do it",
    "confirm",
    "allow",
)
_DENY_WORDS = (
    "deny",
    "no",
    "nope",
    "not",
    "don't",
    "do not",
    "never",
    "cancel",
    "stop",
    "reject",
    "refuse",
    "decline",
    "block",
)


ASK_ASSISTANT: dict[str, Any] = {
    "name": "ask_assistant",
    "description": (
        "Hand the user's request to the full Whisper Studio assistant (Claude), which can do "
        "everything the app can: open folders and workspaces, read and edit files, run "
        "commands and tests, git and branches, documents, transcripts, web research, spawn "
        "agents. Call it as soon as the user asks for something concrete; do not interrogate "
        "the user first, the assistant looks things up itself and asks back only if it must. "
        "Quick tasks return the written answer, which is also shown on the user's screen, so "
        "speak only the gist. Long tasks return 'Working on it' and continue in the "
        "background: tell the user, keep handling other requests, and when the result "
        "arrives as a message starting with [Result of the request ...] summarize it. If a "
        "result says the assistant is waiting for the user's approval or answer, ask the user "
        "out loud and then call resolve_request."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": (
                    "The user's request as one clear written instruction, keeping their exact "
                    "words for names, paths, branches and commands. Names are often spelled out "
                    "in speech: 'm l dash o p s' is the folder ml-ops, 'git branch' is a git "
                    "command. Add conversation context the assistant needs; never add your own "
                    "questions or guesses."
                ),
            }
        },
        "required": ["request"],
    },
}

RESOLVE_REQUEST: dict[str, Any] = {
    "name": "resolve_request",
    "description": (
        "Deliver the user's decision on the assistant's PENDING request and get the continued "
        "answer. Use it ONLY right after an ask_assistant result (or a delivered message) said "
        "the assistant paused and is waiting for the user (approval, question, or folder). For "
        "an approval pass approve or deny; for a question pass the user's answer in their own "
        "words; for a folder pass the folder name or path. For a new or changed request, call "
        "ask_assistant instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "description": "approve, deny, or the user's answer",
            }
        },
        "required": ["decision"],
    },
}

CONTROL_RECORDING: dict[str, Any] = {
    "name": "control_recording",
    "description": "Start or stop the meeting transcription recorder in the app.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop"], "description": "start or stop"}
        },
        "required": ["action"],
    },
}

END_CONVERSATION: dict[str, Any] = {
    "name": "end_conversation",
    "description": (
        "End the voice conversation. Call it when the user says stop, goodbye, that they "
        "are done, or asks to go back to typing."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

BACKGROUND_STATUS: dict[str, Any] = {
    "name": "background_status",
    "description": (
        "What the assistant is doing in the background right now: the requests still running "
        "(with how long they have been going) and the agents they spawned, with each agent's "
        "task and status. Call it ONLY when the user asks what is running, how far along "
        "something is, or about the agents; then tell them in one or two sentences. Never "
        "guess this information."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

VOICE_TOOLS: list[dict[str, Any]] = [
    ASK_ASSISTANT,
    RESOLVE_REQUEST,
    BACKGROUND_STATUS,
    CONTROL_RECORDING,
    END_CONVERSATION,
]


@dataclass
class ToolContext:
    """What a tool run may reach.

    ``pending`` is the assistant's unanswered request (approval, question,
    folder); ``pending_queue`` holds further pauses that arrived while one was
    already waiting. ``runs`` tracks delegated turns in flight
    (run_id -> {request, task, detached, started}). ``deliver_late`` speaks a
    background result to Sonic as a typed turn; ``register_task`` lets the
    session own background tasks for cleanup.
    """

    session_id: str
    model_key: str | None
    emit: EmitFn
    request_end: Callable[[], None]
    pending: dict[str, Any] = field(default_factory=dict)
    session_approvals: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_queue: list[dict[str, Any]] = field(default_factory=list)
    deliver_late: Callable[[str], Awaitable[None]] | None = None
    register_task: Callable[[asyncio.Task], None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


AGENT_TOOLS = frozenset({"spawn_agent", "team_create", "list_agents"})
_FULL_INPUT_CHARS = 4000
_FULL_OUTPUT_CHARS = 8000


def _carry_full(name: str, tool_input: dict[str, Any]) -> bool:
    if name in AGENT_TOOLS:
        return True
    try:
        return len(json.dumps(tool_input, ensure_ascii=False)) <= _FULL_INPUT_CHARS
    except (TypeError, ValueError):
        return False


def _preview(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= _PREVIEW_CHARS else text[: _PREVIEW_CHARS - 1] + "…"


def _cloud_model_key(model_key: str | None) -> str | None:
    """The headless runner refuses on-device models; fall back to the default
    cloud model for those (None lets the runner resolve it)."""
    if not model_key:
        return None
    try:
        from server.local.runtime import is_local_model

        if is_local_model(model_key):
            return None
    except Exception:  # noqa: BLE001 - never let a probe break a tool call
        return None
    return model_key


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _similar_inflight(runs: dict[str, dict[str, Any]], request: str) -> str | None:
    """The in-flight request this one duplicates, if any. Sonic tends to relay
    the same ask twice ("list branches" then "what's happening"); a genuinely
    different request is allowed to run alongside."""
    target = _normalized(request)
    for info in runs.values():
        other = _normalized(str(info.get("request", "")))
        if not other or not target:
            continue
        if other == target or other in target or target in other:
            return str(info["request"])
        if difflib.SequenceMatcher(None, other, target).ratio() >= 0.8:
            return str(info["request"])
    return None


async def run_tool(name: str, tool_input: dict[str, Any], ctx: ToolContext) -> str:
    """Execute one Sonic tool call. Always returns a string (never raises) so
    the session can answer every toolUse, which Sonic requires."""
    try:
        if name == "ask_assistant":
            return await _ask_assistant(str(tool_input.get("request") or "").strip(), ctx)
        if name == "resolve_request":
            return await _resolve_request(str(tool_input.get("decision") or ""), ctx)
        if name == "background_status":
            return _background_status(ctx)
        if name == "control_recording":
            action = str(tool_input.get("action") or "").lower()
            if action not in ("start", "stop"):
                return "Error: action must be start or stop."
            await ctx.emit({"type": "client_action", "action": "recording", "value": action})
            return f"Recording {action} requested. Tell the user briefly."
        if name == "end_conversation":
            ctx.request_end()
            return "Voice conversation is ending. Say a short goodbye."
        return f"Error: unknown tool {name}."
    except Exception as exc:  # noqa: BLE001 - the model must always get an answer
        log.exception("voice tool %s failed", name)
        return f"Error: {type(exc).__name__}: {exc}"


# ── background_status ────────────────────────────────────────────────────────


def _background_status(ctx: ToolContext) -> str:
    """A factual snapshot of the delegated runs in flight and the agents the
    chat session currently has, so Sonic never has to guess."""
    now = time.monotonic()
    lines: list[str] = []
    if ctx.runs:
        lines.append(f"{len(ctx.runs)} request(s) running:")
        for info in ctx.runs.values():
            started = info.get("started")
            age = f" ({int(now - started)} s so far)" if isinstance(started, (int, float)) else ""
            lines.append(f"- {info.get('request', '')}{age}")
    else:
        lines.append("No request is running in the background.")
    if ctx.pending:
        lines.append(
            "One request is paused waiting for the user's decision: "
            f"{ctx.pending.get('summary') or ctx.pending.get('question') or ctx.pending.get('action')}"
        )
    try:
        from server.agents.registry import agent_registry

        agents = agent_registry.list_all(ctx.session_id)
    except Exception:  # noqa: BLE001 - status must never fail
        agents = []
    active = [a for a in agents if getattr(a, "status", "") in ("running", "pending", "starting")]
    recent = [a for a in agents if a not in active]
    if active:
        lines.append(f"{len(active)} agent(s) running:")
        for a in active:
            lines.append(
                f"- {getattr(a, 'agent_type', 'agent')} agent: {_preview(getattr(a, 'task', ''))}"
            )
    if recent:
        lines.append(f"{len(recent)} agent(s) finished recently:")
        for a in recent[-5:]:
            lines.append(
                f"- {getattr(a, 'agent_type', 'agent')} agent ({getattr(a, 'status', '')}): "
                f"{_preview(getattr(a, 'task', ''))}"
            )
    if not active and not recent:
        lines.append("No agents are running.")
    lines.append("Tell the user in one or two sentences; do not read this list verbatim.")
    return "\n".join(lines)


# ── ask_assistant ────────────────────────────────────────────────────────────


async def _ask_assistant(request: str, ctx: ToolContext) -> str:
    if not request:
        return "Error: empty request. Ask the user what they want done."
    if ctx.pending:
        return (
            "Error: the assistant is still waiting on the user's decision for its earlier "
            "request. Ask the user and call resolve_request first."
        )
    dup = _similar_inflight(ctx.runs, request)
    if dup:
        return (
            f"Error: the assistant is already working on that ({dup}). Do not ask again; tell "
            "the user it is in progress and that the result will arrive on its own."
        )
    if len(ctx.runs) >= MAX_CONCURRENT_RUNS:
        return (
            f"Error: {MAX_CONCURRENT_RUNS} requests are already running. Ask the user to wait "
            "for one of them to finish."
        )
    from server.exec.headless import run_headless_turn

    run_id = f"voice-{ctx.session_id}-{uuid.uuid4().hex[:8]}"
    # The run belongs to the chat session (agents, costs, tool activations are
    # shared with typed chat, so list_agents sees voice-spawned agents); only
    # its pause slot is per run.
    gen = run_headless_turn(
        request,
        model_key=_cloud_model_key(ctx.model_key),
        ephemeral=True,
        session_id=ctx.session_id,
        scope_id=run_id,
        event_channel=f"voice:{run_id}",
        max_rounds=ASSISTANT_MAX_ROUNDS,
        attended=True,
        session_approvals=ctx.session_approvals,
        system_hint=VOICE_HINT,
    )
    return await _launch(gen, ctx, run_id, request[:120])


VOICE_HINT = (
    "This request was relayed from a spoken conversation through speech recognition. Names "
    "may be spelled out letter by letter ('m l dash o p s' means ml-ops) or misheard ('brands' "
    "next to 'git' means branches). Resolve names against what actually exists (folders, "
    "files, branches, commands) rather than asking; ws_open_folder looks folders up by name. "
    "When several things plausibly match, ask with ask_user_question. When the user asks to "
    "open a folder, open it (pass switch=true if another workspace is connected); never "
    "re-open a folder that is already the connected workspace. Prefer the dedicated tools "
    "(git_branch_list, git_status, ws_list_directory) over shell commands, and call each "
    "tool once. Speech recognition also confuses pull with push, delete with delegate, and "
    "similar pairs: before any operation that changes a remote, deletes or force-overwrites "
    "something (git push, force push, branch or file deletion, reset, rm), confirm the exact "
    "operation and target with ask_user_question, and never substitute a different operation "
    "for the one requested. Report only what tool results confirm; if something did not run, "
    "say so. Keep the written answer short and useful, no preamble, and never repeat the same "
    "output twice."
)


async def _launch(
    gen: AsyncIterator[dict[str, Any]], ctx: ToolContext, run_id: str, label: str
) -> str:
    """Start driving a delegated turn. Return its answer if it finishes within
    ASK_WAIT_S; otherwise leave it running in the background (its outcome is
    typed to Sonic by ``_deliver_detached`` when it lands) and return a short
    "working on it" note."""
    info: dict[str, Any] = {
        "request": label,
        "detached": False,
        "started": time.monotonic(),
        "task": None,
    }
    ctx.runs[run_id] = info
    task = asyncio.create_task(_drive(gen, ctx, run_id, label))
    info["task"] = task
    if ctx.register_task:
        ctx.register_task(task)
    await asyncio.wait({task}, timeout=ASK_WAIT_S)
    if task.done():
        return _task_result(task)
    # From here on the run is on its own. Its outcome is delivered to Sonic by
    # the done callback, which fires only once the run has fully finished, so
    # nothing can fall between this timer and the run's last step.
    info["detached"] = True
    task.add_done_callback(lambda t: _schedule_late_delivery(ctx, info, t))
    return (
        f"{WORKING_PREFIX} {label}. It is running in the background and may take minutes; "
        "the result will be delivered to you automatically as a message starting with "
        "[Result of the request ...]. Tell the user briefly and keep handling other requests."
    )


def _schedule_late_delivery(ctx: ToolContext, info: dict[str, Any], task: asyncio.Task) -> None:
    if task.cancelled() or ctx.deliver_late is None:
        return
    delivery = asyncio.ensure_future(_deliver_detached(ctx, info, task))
    if ctx.register_task:
        ctx.register_task(delivery)


async def _deliver_detached(ctx: ToolContext, info: dict[str, Any], task: asyncio.Task) -> None:
    """Type a detached run's outcome to Sonic: an answer to summarize, a pause
    to ask the user about, or a note that its pause is queued."""
    text = _task_result(task)
    label = str(info.get("request", ""))
    outcome = info.get("outcome", "answer")
    if outcome == "paused":
        note = f"[The assistant needs the user's decision for the request '{label}':] {text}"
    elif outcome == "queued":
        note = f"[About the request '{label}':] {text}"
    else:
        spoken = text if len(text) <= _LATE_NOTE_CHARS else text[: _LATE_NOTE_CHARS - 1] + "…"
        note = (
            f"[Result of the request '{label}'. Summarize it for the user in one or two "
            "sentences; the full text is already on their screen. Do not call any tool for "
            f"this:] {spoken}"
        )
    if ctx.deliver_late is not None:
        await ctx.deliver_late(note)


def _task_result(task: asyncio.Task) -> str:
    try:
        return str(task.result())
    except asyncio.CancelledError:
        return "Error: the request was cancelled."
    except Exception as exc:  # noqa: BLE001 - reported to the model, never raised
        log.exception("voice: delegated run crashed")
        return f"Error: {type(exc).__name__}: {exc}"


async def _drive(
    gen: AsyncIterator[dict[str, Any]], ctx: ToolContext, run_id: str, request_label: str = ""
) -> str:
    """Consume one delegated turn's events: forward progress to the browser,
    collect the written answer (emitted as assistant_answer), park a pause
    (approval/question) for resolve_request. Records the outcome kind on the
    run's info so a detached run's late delivery can word it right."""
    texts: list[str] = []
    errors: list[str] = []
    status = "completed"
    pending_req: dict[str, Any] | None = None
    deadline = time.monotonic() + ASSISTANT_DEADLINE_S
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "timeout"
                break
            try:
                ev = await asyncio.wait_for(gen.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                status = "timeout"
                break
            kind = ev.get("type")
            if kind == "text" and ev.get("text"):
                texts.append(str(ev["text"]))
            elif kind == "tool_call":
                name = str(ev.get("name", ""))
                step: dict[str, Any] = {
                    "type": "assistant_step",
                    "run_id": run_id,
                    "name": name,
                    "status": "running",
                    "detail": _preview(ev.get("input", {})),
                }
                tool_input = ev.get("input")
                if isinstance(tool_input, dict) and _carry_full(name, tool_input):
                    # The browser's agent cards need the real task and type.
                    step["input"] = tool_input
                await ctx.emit(step)
            elif kind == "tool_result":
                name = str(ev.get("name", ""))
                output = str(ev.get("output", ""))
                step = {
                    "type": "assistant_step",
                    "run_id": run_id,
                    "name": name,
                    "status": "error" if ev.get("status") == "error" else "ok",
                    "detail": _preview(output),
                }
                if name in AGENT_TOOLS and len(output) <= _FULL_OUTPUT_CHARS:
                    # The result JSON carries the team id the cards anchor to.
                    step["output"] = output
                await ctx.emit(step)
            elif kind == "team_progress" and isinstance(ev.get("event"), dict):
                # Agent progress (turns, tool calls, completion) rendered with
                # the same cards typed chat uses.
                await ctx.emit({"type": "team_progress", "run_id": run_id, "event": ev["event"]})
            elif kind in ("approval_request", "user_question", "workspace_prompt"):
                pending_req = {**ev, "run_id": run_id, "label": request_label}
            elif kind == "folder_opened" and ev.get("path"):
                await ctx.emit(
                    {"type": "client_action", "action": "workspace", "value": str(ev["path"])}
                )
            elif kind == "error" and ev.get("message"):
                errors.append(str(ev["message"]))
            elif kind == "done":
                status = str(ev.get("status", "completed"))
    except asyncio.CancelledError:
        # Stopped by the user (or the browser went away): the browser keeps the
        # steps shown so far as a stopped trace, like a stopped chat turn.
        with contextlib.suppress(Exception):
            await ctx.emit(
                {
                    "type": "assistant_answer",
                    "run_id": run_id,
                    "request": request_label,
                    "output": "(Stopped)",
                    "status": "stopped",
                }
            )
        raise
    finally:
        info = ctx.runs.pop(run_id, None) or {}
        with contextlib.suppress(Exception):
            await gen.aclose()

    if pending_req is not None and status == "paused":
        if ctx.pending:
            # Another request already holds the card and resolve_request; this
            # one waits its turn (see _announce_next_pending). Sonic must not
            # ask about it yet, or the user's answer would land on the wrong one.
            ctx.pending_queue.append(pending_req)
            info["outcome"] = "queued"
            return (
                f"The assistant also needs the user's decision for the request "
                f"'{request_label}', but the earlier request must be answered first and is "
                f"still pending. {_describe_request(ctx.pending, [])} The second request "
                "will be raised once this one is resolved."
            )
        ctx.pending.clear()
        ctx.pending.update(pending_req)
        await ctx.emit(_request_event(pending_req))
        info["outcome"] = "paused"
        return _describe_request(pending_req, texts)

    answer = "\n\n".join(t.strip() for t in texts if t.strip()).strip()
    if status == "timeout":
        note = (
            "The assistant hit the time limit and was stopped; this is what it had so far."
            if answer
            else "The assistant hit the time limit before producing an answer."
        )
        answer = f"{note}\n\n{answer}" if answer else note
    elif not answer:
        if errors:
            answer = "The assistant could not complete this: " + "; ".join(errors)
        elif status == "turn_limit":
            answer = "The assistant ran out of steps before finishing. Ask to continue."
        else:
            answer = "The assistant finished without a written answer."
    elif status == "turn_limit":
        answer += "\n\n(The assistant stopped at its step limit; the task may be unfinished.)"
    if len(answer) > MAX_RESULT_CHARS:
        answer = answer[: MAX_RESULT_CHARS - 1] + "…"

    await ctx.emit(
        {
            "type": "assistant_answer",
            "run_id": run_id,
            "request": request_label,
            "output": answer,
            "status": "error" if (errors and not texts) else "ok",
        }
    )
    info["outcome"] = "answer"
    return answer


def _request_event(req: dict[str, Any]) -> dict[str, Any]:
    kind = req["type"]
    payload = req.get("payload") or {}
    return {
        "type": "assistant_request",
        "kind": kind,
        "run_id": str(req.get("run_id", "")),
        "tool_use_id": str(req.get("tool_use_id", "")),
        "action": str(req.get("action", "")),
        "category": str(req.get("category", "")),
        "summary": str(req.get("summary") or req.get("question") or "").strip()
        or ("Choose a workspace folder" if kind == "workspace_prompt" else ""),
        "question": str(req.get("question", "")),
        "options": [str(o) for o in (req.get("options") or [])],
        "risk_hint": req.get("risk_hint"),
        "detail": str(payload.get("command") or payload.get("path") or ""),
    }


def _describe_request(req: dict[str, Any], texts: list[str]) -> str:
    kind = req["type"]
    said = " ".join(" ".join(t.split()) for t in texts if t.strip())
    prefix = f"The assistant said: {said[:300]} " if said else ""
    if kind == "approval_request":
        risk = req.get("risk_hint")
        risk_note = f" (risk: {risk})" if risk else ""
        return (
            f"{prefix}The assistant paused: it needs the user's approval to "
            f"{req.get('summary') or req.get('action')}{risk_note}. Ask the user yes or no, "
            "then call resolve_request with approve or deny."
        )
    if kind == "user_question":
        options = req.get("options") or []
        opts = f" Options: {', '.join(options)}." if options else ""
        return (
            f"{prefix}The assistant asks the user: {req.get('question')}{opts} Ask the user, "
            "then call resolve_request with their answer."
        )
    return (
        f"{prefix}The assistant needs a workspace folder to work in. Ask the user which "
        "folder (a name or a path), then call resolve_request with it."
    )


# ── resolve_request ──────────────────────────────────────────────────────────


def _classify_decision(decision: str) -> str | None:
    """Map the user's words to approve / deny, or None when unclear.

    Any refusal word anywhere wins ("okay, no" and "yes but don't" are a no):
    a wrongly denied action costs one more question, a wrongly approved one
    can cost a file. Approval needs an explicit approval word and no refusal.
    """
    d = " ".join(decision.lower().split()).strip(" .!,")
    if not d:
        return None
    padded = f" {d} "
    if any(f" {w} " in padded or d.startswith(f"{w} ") or d == w for w in _DENY_WORDS):
        return "deny"
    if any(f" {w} " in padded or d.startswith(f"{w} ") or d == w for w in _APPROVE_WORDS):
        return "approve"
    return None


async def _execute_approval(action: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """Run the approval's registered executor (what POST /api/approval/execute
    does when the user clicks Yes). Returns (ok, detail)."""
    from server.approval import registry

    spec = registry.get(action)
    if spec is None:
        return False, f"No approval registered for action {action!r}."
    try:
        outcome = await spec.executor(payload or {})
    except Exception as exc:  # noqa: BLE001 - executor is user-registered
        log.exception("voice resolve_request: executor %s crashed", action)
        return False, f"Executor crashed: {exc}"
    if outcome.ok and spec.category in ("write", "delete", "cli", "office-script"):
        with contextlib.suppress(Exception):
            from server.git.router import invalidate_changes_cache

            invalidate_changes_cache()
    if outcome.ok:
        return True, outcome.output or ""
    detail = "\n\n".join(part for part in (outcome.error, outcome.output) if part)
    return False, detail or "unknown error"


async def _resolve_request(decision: str, ctx: ToolContext) -> str:
    req = dict(ctx.pending)
    if not req:
        if ctx.runs:
            labels = ", ".join(str(i.get("request", "")) for i in ctx.runs.values())
            return (
                f"Error: nothing to resolve yet; the assistant is still working on: {labels}. "
                "Wait for its answer; it will say if it needs the user's decision."
            )
        return (
            "Error: there is no pending request to resolve (it may already have been answered "
            "on screen). Do not retry; if the user wants something new, call ask_assistant."
        )
    kind = req.get("type")
    action = str(req.get("action", ""))
    payload = req.get("payload") or {}
    answer = decision.strip()
    if not answer:
        return "Error: pass the user's decision."

    if kind == "approval_request" and _classify_decision(answer) is None:
        return "Error: for an approval, pass approve or deny."
    # Claim the request now, before the first await: a spoken "yes" and a tap
    # on the card can arrive together, and the action must run exactly once.
    # The second caller finds nothing pending and is told not to retry.
    ctx.pending.clear()

    if kind == "approval_request":
        verdict = _classify_decision(answer)
        target = str(payload.get("path") or payload.get("command") or req.get("summary") or "N/A")
        if verdict == "deny":
            content = (
                f"[User denied] {action}: {target}. The user rejected this action; it did not run."
            )
        else:
            ws_before = _workspace_path()
            ok, detail = await _execute_approval(action, payload)
            if ok:
                extra = f"\n\n{detail}" if detail else ""
                content = f"[User approved] {action}: {target}. The action succeeded.{extra}"
                ws_after = _workspace_path()
                if ws_after and ws_after != ws_before:
                    await ctx.emit(
                        {"type": "client_action", "action": "workspace", "value": ws_after}
                    )
            else:
                content = (
                    f"[User approved but the operation FAILED] {action}: {target}. Error: {detail}"
                )
    elif kind == "user_question":
        content = answer
    else:  # workspace_prompt
        candidate = os.path.expanduser(answer)
        if os.path.isdir(candidate):
            from server.workspace.state import connect_workspace

            real = connect_workspace(candidate)
            await ctx.emit({"type": "client_action", "action": "workspace", "value": real})
            content = f"[Workspace connected] {real}. Retry the operation."
        else:
            content = f"The user answered: {answer}"

    await ctx.emit(
        {
            "type": "assistant_request_resolved",
            "tool_use_id": str(req.get("tool_use_id", "")),
            "decision": answer,
        }
    )
    from server.exec.headless import run_headless_turn

    label = str(req.get("label") or "the request you decided on")
    gen = run_headless_turn(
        "",
        model_key=_cloud_model_key(ctx.model_key),
        ephemeral=True,
        session_id=ctx.session_id,
        scope_id=str(req["run_id"]),
        event_channel=f"voice:{req['run_id']}",
        max_rounds=ASSISTANT_MAX_ROUNDS,
        attended=True,
        session_approvals=ctx.session_approvals,
        system_hint=VOICE_HINT,
        resume={"answers": [{"tool_use_id": str(req.get("tool_use_id", "")), "content": content}]},
    )
    result = await _launch(gen, ctx, str(req["run_id"]), label)
    await _announce_next_pending(ctx)
    return result


async def _announce_next_pending(ctx: ToolContext) -> None:
    """A pause that queued up behind the one just resolved becomes current."""
    if ctx.pending or not ctx.pending_queue:
        return
    nxt = ctx.pending_queue.pop(0)
    ctx.pending.update(nxt)
    await ctx.emit(_request_event(nxt))
    if ctx.deliver_late:
        await ctx.deliver_late(
            f"[Another request needs the user's decision ('{nxt.get('label', '')}'):] "
            + _describe_request(nxt, [])
        )


def _workspace_path() -> str | None:
    try:
        from server.workspace import get_workspace_path

        return get_workspace_path()
    except Exception:  # noqa: BLE001
        return None
