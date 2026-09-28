"""Wake the parent when agent reports land with no turn to read them.

A team or spawned agent that finishes inside a live turn hands its report to
the model as a tool result, and the model answers. When the reports arrive
after that turn has ended (a background or resumed agent), they used to sit
in the chat as a card until the user typed something. This module gives the
parent its turn anyway. A team or agent whose launching turn the user stopped
wakes nobody: its reports are kept as a row the next message reads
(server.tasks.events.emit_agent_report with ``wake=False``).

- a chat turn is still running: the reports are folded into it through the
  mid-turn inbox, framed as agent output (never as something the user said).
  A turn that ends before another round reads them hands them back here
  (``deliver_unread``), and they get a wake turn like any other;
- no turn is running: a synthesis turn starts on the session's model, with
  the original request and the reports as its prompt, and its answer is
  persisted as an ``agent_answer`` row (backend-owned, shown as an assistant
  bubble, read back by the model as its own previous message);
- a wake turn is already running: it is one model call that cannot read
  anything sent mid-call, so the reports wait here and one follow-up wake
  answers everything that landed meanwhile once it ends.

A wake turn never runs beside a chat turn. When one starts in the session
(the user sent a message, or approved a paused turn) and passes model
resolution, the wake is cancelled and its reports go to that turn
(``yield_to_chat_turn``). A turn refused at model resolution leaves the wake
to answer.

One wake per session at a time, gated by the session cost budget and by the
``wake_parent_on_reports`` config switch. The synthesis is a headless turn,
which runs on a cloud model only: in Local mode (nothing leaves this Mac) and
for agents that ran on the on-device model there is no wake turn, and a
notification says the reports are in the chat for the next message. A wake
that fails says so in a notification too; an error is never persisted as the
assistant's answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from server.agents.journal import REPORT_CHARS
from server.infrastructure.async_tasks import spawn
from server.infrastructure.config import load_config
from server.notifications import record_notification
from server.tasks.events import emit_session_event

log = logging.getLogger("whisper-studio")

# The wake turn synthesizes; it never opens new lines of work. One round with
# no tools is exactly that.
WAKE_MAX_ROUNDS = 1
# Bound on the original request and previous answer quoted into the prompt.
CONTEXT_CHARS = 4000
ANSWER_CHARS = 20000

SYSTEM_HINT = (
    "This turn was started by the runtime, not by the user: the agents you "
    "launched finished after your previous turn had ended, and their reports "
    "are in the message. Reply as your next message in the conversation, "
    "answering the user's original request from the reports. Do not call tools, "
    "start agents, or ask the user to wait."
)

# The running wake turn per session, and the report payload it answers.
_tasks: dict[str, asyncio.Task] = {}
_answering: dict[str, dict] = {}
# Report payloads that landed while a wake turn ran, per session. The chat
# inbox is not the place for them: no chat turn is there to read it, and the
# wake turn itself never does.
_late: dict[str, list[dict]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def enabled() -> bool:
    try:
        return bool(load_config().get("wake_parent_on_reports", True))
    except Exception:  # noqa: BLE001 - config trouble must not block delivery
        return True


def turn_is_live(session_id: str) -> bool:
    """Whether a chat turn is currently streaming for this session (the same
    slot and heartbeat /api/chat uses to queue mid-turn messages)."""
    from server.chat import stream_slot

    return stream_slot.is_live(session_id)


def reports_text(payload: dict) -> str:
    """The reports rendered the way the model reads an agent_report row."""
    from server.infrastructure.sessions import _agent_report_prompt_view

    return _agent_report_prompt_view({"agentReport": payload})["content"]


def maybe_wake(session_id: str, payload: dict, *, previous_answer: str = "") -> str:
    """Deliver freshly landed reports to the parent. Returns how: ``live``
    (folded into the running chat turn), ``scheduled`` (a wake turn was
    started), ``queued`` (a wake turn is running; a follow-up answers these)
    or ``skipped:<reason>``.

    ``previous_answer`` is what the wake before this one just answered, for a
    follow-up wake: its agent_answer row is still being written when the
    follow-up reads the history, so it is handed over directly."""
    if not session_id or not isinstance(payload, dict):
        return "skipped:no-session"
    if not enabled():
        return "skipped:disabled"
    from server.chat.engine.midturn_inbox import AGENT_REPORT, push

    text = reports_text(payload)
    # A live chat turn that has already taken its last look at the inbox
    # refuses the push: it is ending, so the reports get a wake turn instead.
    if turn_is_live(session_id) and push(
        session_id, text[: REPORT_CHARS * 2], kind=AGENT_REPORT, payload=payload
    ):
        return "live"
    if session_id in _tasks:
        _late.setdefault(session_id, []).append(payload)
        return "queued"
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "skipped:no-loop"
    _answering[session_id] = payload
    _tasks[session_id] = spawn(
        _wake(session_id, payload, previous_answer), name=f"wake-parent-{session_id[:8]}"
    )
    return "scheduled"


def yield_to_chat_turn(session_id: str, *, fresh: bool) -> None:
    """A chat turn holds the session and its model resolved (the route calls
    this once the turn is sure to run). A wake turn running beside it would
    answer the same reports a second time, as a second concurrent turn in the
    session, so it is cancelled and the chat turn takes the reports. A fresh
    turn already has them: every report is persisted as an agent_report row
    before a wake is scheduled, and the client ships those rows in its
    history (src/hooks/chatStream/history.ts) for visible_chat_history to
    relabel. An approval continuation runs on its stashed conversation
    instead, so they are handed to its inbox, framed as agent output."""
    task = _tasks.pop(session_id, None)
    payloads = [p for p in (_answering.pop(session_id, None),) if p]
    payloads += _late.pop(session_id, [])
    if task is not None and not task.done():
        task.cancel()
        log.info("wake turn for session %s gave way to a chat turn", session_id)
    if fresh:
        return
    from server.chat.engine.midturn_inbox import AGENT_REPORT, push

    for p in payloads:
        push(session_id, reports_text(p)[: REPORT_CHARS * 2], kind=AGENT_REPORT, payload=p)


def deliver_unread(session_id: str, payloads: list[dict]) -> str:
    """Reports a chat turn accepted into its inbox and ended without reading.
    That turn has closed its inbox, so they get a wake turn, the same as a
    push it refused."""
    if not payloads:
        return "skipped:none"
    return maybe_wake(session_id, _merged(payloads))


def _merged(payloads: list[dict]) -> dict:
    """Several report payloads as one, so one follow-up wake answers them all."""
    if len(payloads) == 1:
        return payloads[0]
    names = [str(p.get("team_name") or "agents") for p in payloads]
    reasons = [str(p.get("reason") or "") for p in payloads]
    return {
        **payloads[0],
        "team_name": ", ".join(dict.fromkeys(names)),
        "reason": "; ".join(dict.fromkeys(r for r in reasons if r)),
        "agents": [a for p in payloads for a in (p.get("agents") or [])],
    }


def _history_for(session_id: str) -> tuple[str, str]:
    """(last user request, last assistant text) from the stored history."""
    from server.infrastructure.sessions import _get_conn

    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT chat_history FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        history = json.loads(row["chat_history"]) if row and row["chat_history"] else []
    except Exception:  # noqa: BLE001 - no history is not a reason to stay silent
        history = []
    last_user = ""
    last_assistant = ""
    for m in reversed(history):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "agent_answer":
            # A previous wake's answer is the assistant's own last message, so
            # a follow-up wake builds on it instead of repeating it.
            role, content = "assistant", (m.get("agentAnswer") or {}).get("text")
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user" and not last_user:
            last_user = content.strip()
        elif role == "assistant" and not last_assistant and not last_user:
            last_assistant = content.strip()
        if last_user:
            break
    return last_user[-CONTEXT_CHARS:], last_assistant[-CONTEXT_CHARS // 2 :]


def _agent_model_ids(payload: dict) -> list[str]:
    """The model ids the reporting agents ran on, as their journals record
    them (team members inherit the session's model)."""
    try:
        from server.agents import journal as journal_mod

        ids: list[str] = []
        for a in payload.get("agents") or []:
            rec = journal_mod.load(str(a.get("agent_id") or ""), payload.get("session_id"))
            model_id = str(((rec or {}).get("meta") or {}).get("model") or "")
            if model_id and model_id not in ids:
                ids.append(model_id)
        return ids
    except Exception:  # noqa: BLE001 - no journal is not a reason to stay silent
        return []


def _model_key_for(model_ids: list[str]) -> str | None:
    """The chat model key the agents ran on, so the answer comes from the
    model the user chose. Resolved through the whole chat catalog, which
    includes downloaded on-device models that config chat_models never lists.
    None falls back to the default chat model."""
    if not model_ids:
        return None
    try:
        from server.chat.infra import _get_chat_models

        for key, entry in _get_chat_models().items():
            if isinstance(entry, str):
                if entry == model_ids[0]:
                    return str(key)
                continue
            if isinstance(entry, dict) and model_ids[0] in (
                entry.get("id"),
                entry.get("model_id"),
                entry.get("model"),
            ):
                return str(key)
    except Exception:  # noqa: BLE001
        return None
    return None


def _why_no_wake_turn(model_ids: list[str]) -> str | None:
    """Why the synthesis turn cannot run for this session, or None when it can.
    It never falls back to another model: the reports stay in the chat.

    Decided from the journaled model ids themselves, not from a catalog
    lookup: a downloaded on-device model has no config entry, and a lookup
    that missed it sent the on-device agents' work to the cloud default."""
    from server.infrastructure.model_mode import current_mode
    from server.local.runtime import is_local_model_id

    if current_mode(load_config()) == "local":
        return (
            "Local mode keeps every model call on this Mac, and the automatic "
            "answer to agent reports runs on a cloud model."
        )
    if any(is_local_model_id(m) for m in model_ids):
        return (
            "The agents ran on the on-device model, and the automatic answer to "
            "agent reports runs on a cloud model."
        )
    return None


def build_prompt(session_id: str, payload: dict, previous_answer: str = "") -> str:
    last_user, last_assistant = _history_for(session_id)
    parts = [
        "[Runtime wake, not typed by the user] The agents launched from this "
        "conversation finished after the turn that started them had ended. Their "
        "reports are below. Answer the user's original request now, in your own "
        "voice, as the reply the user is waiting for: lead with the answer, say what "
        "the agents found and how confident each report is, and name what remains "
        "open. Do not start new agents or call tools.",
    ]
    if last_user:
        parts.append("Original request:\n" + last_user)
    if previous_answer:
        parts.append(
            "Your last message, which answered the reports that came in before these:\n"
            + previous_answer.strip()[-CONTEXT_CHARS // 2 :]
            + "\n\nThe reports below arrived after it. Build on that answer: say what "
            "they add or change, without repeating it."
        )
    elif last_assistant:
        parts.append("Your last message before the agents finished:\n" + last_assistant)
    parts.append(reports_text(payload))
    return "\n\n".join(parts)


async def _wake(session_id: str, payload: dict, previous_answer: str = "") -> None:
    team_name = str(payload.get("team_name") or "agents")
    answer = ""
    try:
        from server.costs.budget import check_budget

        exceeded = check_budget(session_id)
        if exceeded:
            record_notification(
                session_id=session_id,
                source="agents",
                title=f"Reports from {team_name} not answered",
                message=f"The session cost budget is reached ({exceeded.message}). "
                "The reports are in the chat; your next message can use them.",
                status="warning",
            )
            return
        model_ids = _agent_model_ids(payload)
        why_not = _why_no_wake_turn(model_ids)
        if why_not:
            record_notification(
                session_id=session_id,
                source="agents",
                title=f"Reports from {team_name} are in the chat",
                message=f"{why_not} Your next message can use them.",
                status="info",
            )
            return
        from server.exec.headless import run_headless_turn

        model_key = _model_key_for(model_ids)
        prompt = build_prompt(session_id, payload, previous_answer)
        text_parts: list[str] = []
        errors: list[str] = []
        status = "failed"
        async for ev in run_headless_turn(
            prompt,
            session_id=session_id,
            ephemeral=True,
            model_key=model_key,
            max_rounds=WAKE_MAX_ROUNDS,
            system_hint=SYSTEM_HINT,
            scope_id=f"wake:{session_id}",
            cost_source="wake",
        ):
            kind = ev.get("type")
            if kind == "text":
                text_parts.append(str(ev.get("text") or ""))
            elif kind == "error":
                errors.append(str(ev.get("message") or "unknown error"))
            elif kind == "done":
                status = str(ev.get("status") or status)
        text = "".join(text_parts).strip()
        if errors or status == "failed" or not text:
            # An agent_answer row is read back as the assistant's own words,
            # so a failure goes to a notification instead.
            reason = errors[0] if errors else f"no answer came back (status: {status})"
            record_notification(
                session_id=session_id,
                source="agents",
                title=f"Reports from {team_name} not answered",
                message=f"The automatic answer failed: {reason[:500]}. "
                "The reports are in the chat; your next message can use them.",
                status="warning",
            )
            return
        answer = text[:ANSWER_CHARS]
        emit_session_event(
            session_id,
            role="agent_answer",
            payload_key="agentAnswer",
            payload={
                "text": answer,
                "team_id": payload.get("team_id"),
                "team_name": team_name,
                "model": model_key,
                "timestamp": _now(),
            },
        )
        record_notification(
            session_id=session_id,
            source="agents",
            title=f"Answer ready: {team_name}",
            message=text[:300],
        )
    except Exception as e:  # noqa: BLE001 - the reports are already in the chat
        log.warning("wake turn for session %s failed: %s", session_id, e)
    finally:
        # A wake a chat turn superseded was already cleared by
        # yield_to_chat_turn, and a later wake may own the session by now.
        if _tasks.get(session_id) is asyncio.current_task():
            _tasks.pop(session_id, None)
            _answering.pop(session_id, None)
            # Reports that landed during this wake get their own, in the same
            # synchronous step, so nothing lands between the two unanswered.
            # The follow-up passes the switch and the budget gate again, and
            # goes into a chat turn instead if one has started meanwhile. It
            # is handed this wake's answer: the agent_answer row above is
            # written through the executor, after the follow-up has already
            # read the history.
            late = _late.pop(session_id, [])
            if late:
                try:
                    maybe_wake(session_id, _merged(late), previous_answer=answer or previous_answer)
                except Exception as e:  # noqa: BLE001 - the rows are already in the chat
                    log.warning("follow-up wake for session %s skipped: %s", session_id, e)
