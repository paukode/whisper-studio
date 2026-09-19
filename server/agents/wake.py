"""Wake the parent when agent reports land with no turn to read them.

A team or spawned agent that finishes inside a live turn hands its report to
the model as a tool result, and the model answers. When the reports arrive
after that turn has ended (a cancelled team, a background or resumed agent),
they used to sit in the chat as a card until the user typed something. This
module gives the parent its turn anyway:

- a turn is still running: the reports are folded into it through the
  mid-turn inbox, exactly like a message the user types while it works;
- no turn is running: a synthesis turn starts on the session's model, with
  the original request and the reports as its prompt, and its answer is
  persisted as an ``agent_answer`` row (backend-owned, shown as an assistant
  bubble, read back by the model as its own previous message).

One wake per session at a time, gated by the session cost budget and by the
``wake_parent_on_reports`` config switch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
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

_waking: set[str] = set()
_tasks: dict[str, asyncio.Task] = {}


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
    from server.chat import routes

    busy = routes._active_chat_streams.get(session_id)
    if busy is None:
        return False
    last = routes._stream_heartbeat.get(session_id, busy)
    return time.monotonic() - last < routes._STREAM_STALE_AFTER_S


def reports_text(payload: dict) -> str:
    """The reports rendered the way the model reads an agent_report row."""
    from server.infrastructure.sessions import _agent_report_prompt_view

    return _agent_report_prompt_view({"agentReport": payload})["content"]


def maybe_wake(session_id: str, payload: dict) -> str:
    """Deliver freshly landed reports to the parent. Returns how: ``live``
    (folded into the running turn), ``scheduled`` (a wake turn was started)
    or ``skipped:<reason>``."""
    if not session_id or not isinstance(payload, dict):
        return "skipped:no-session"
    if not enabled():
        return "skipped:disabled"
    text = reports_text(payload)
    if turn_is_live(session_id) or session_id in _waking:
        from server.chat.engine.midturn_inbox import push

        push(session_id, text[: REPORT_CHARS * 2])
        return "live"
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "skipped:no-loop"
    _waking.add(session_id)
    _tasks[session_id] = spawn(_wake(session_id, payload), name=f"wake-parent-{session_id[:8]}")
    return "scheduled"


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
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text", ""))
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        if m.get("role") == "user" and not last_user:
            last_user = content.strip()
        elif m.get("role") == "assistant" and not last_assistant and not last_user:
            last_assistant = content.strip()
        if last_user:
            break
    return last_user[-CONTEXT_CHARS:], last_assistant[-CONTEXT_CHARS // 2 :]


def _model_key_for(payload: dict) -> str | None:
    """The chat_models key the agents ran on (team members inherit the
    session's model), so the answer comes from the same model the user chose.
    None falls back to the default chat model."""
    try:
        from server.agents import journal as journal_mod

        model_id = ""
        for a in payload.get("agents") or []:
            rec = journal_mod.load(str(a.get("agent_id") or ""), payload.get("session_id"))
            model_id = str(((rec or {}).get("meta") or {}).get("model") or "")
            if model_id:
                break
        if not model_id:
            return None
        chat_models = load_config().get("chat_models") or {}
        for key, entry in chat_models.items():
            if isinstance(entry, str):
                if entry == model_id:
                    return str(key)
                continue
            if isinstance(entry, dict) and model_id in (
                entry.get("id"),
                entry.get("model_id"),
                entry.get("model"),
            ):
                return str(key)
    except Exception:  # noqa: BLE001
        return None
    return None


def build_prompt(session_id: str, payload: dict) -> str:
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
    if last_assistant:
        parts.append("Your last message before the agents finished:\n" + last_assistant)
    parts.append(reports_text(payload))
    return "\n\n".join(parts)


async def _wake(session_id: str, payload: dict) -> None:
    team_name = str(payload.get("team_name") or "agents")
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
        from server.exec.headless import run_headless_turn

        prompt = build_prompt(session_id, payload)
        model_key = _model_key_for(payload)
        text_parts: list[str] = []
        status = "failed"
        async for ev in run_headless_turn(
            prompt,
            session_id=session_id,
            ephemeral=True,
            model_key=model_key,
            max_rounds=WAKE_MAX_ROUNDS,
            system_hint=SYSTEM_HINT,
            scope_id=f"wake:{session_id}",
        ):
            kind = ev.get("type")
            if kind == "text":
                text_parts.append(str(ev.get("text") or ""))
            elif kind == "error":
                text_parts.append(f"\n\n[The wake turn hit an error: {ev.get('message')}]")
            elif kind == "done":
                status = str(ev.get("status") or status)
        text = "".join(text_parts).strip()
        if not text:
            text = (
                "The agents' reports are above, but the runtime could not produce an answer "
                f"from them (status: {status}). Ask me about them and I will."
            )
        emit_session_event(
            session_id,
            role="agent_answer",
            payload_key="agentAnswer",
            payload={
                "text": text[:ANSWER_CHARS],
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
        _waking.discard(session_id)
        _tasks.pop(session_id, None)
