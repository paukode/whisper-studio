"""Next-turn injection of detached-task completions.

A detached agent finishing while no turn is in flight already lands a task
card in the chat (UI). This module closes the MODEL's side of the loop: at
the start of the next turn, completed-but-undelivered background tasks are
summarized in a leading text block inside the new user message, so the model
learns the outcome without polling — on every provider, because injection
happens before the local/OpenAI/Anthropic dispatch split.

A leading block inside the EXISTING user message (never a separate
consecutive user message) keeps Bedrock's role alternation valid and
perturbs the prompt-cache prefix as little as possible.
"""

import json
import logging

log = logging.getLogger("whisper-studio")

MAX_INJECTED = 5
SUMMARY_CHARS = 300


def _terminal_paragraph(text: str, limit: int = SUMMARY_CHARS) -> str:
    s = (text or "").strip()
    if not s:
        return "(no output)"
    last = s.split("\n\n")[-1].strip() or s
    return last[:limit] + ("…" if len(last) > limit else "")


def pending_completions(session_id: str) -> list[dict]:
    """Terminal, undelivered detached-agent/workflow tasks for a session."""
    if not session_id:
        return []
    try:
        from server.tasks import registry

        rows = registry.list_tasks(session_id=session_id, limit=50)
    except Exception:
        return []
    out = []
    for t in rows:
        if t.get("kind") not in ("agent", "workflow"):
            continue
        if t.get("status") not in ("completed", "failed", "stopped", "interrupted"):
            continue
        if (t.get("meta") or {}).get("delivered"):
            continue
        out.append(t)
    return out[:MAX_INJECTED]


def mark_delivered(task_ids: list[str]) -> None:
    if not task_ids:
        return
    try:
        from server.tasks import registry

        with registry._get_conn() as conn:
            for tid in task_ids:
                row = conn.execute(
                    "SELECT meta FROM agent_tasks WHERE task_id=?", (tid,)
                ).fetchone()
                if row is None:
                    continue
                try:
                    meta = json.loads(row["meta"]) if row["meta"] else {}
                except (TypeError, ValueError):
                    meta = {}
                meta["delivered"] = True
                conn.execute(
                    "UPDATE agent_tasks SET meta=? WHERE task_id=?",
                    (json.dumps(meta), tid),
                )
    except Exception as e:
        log.warning("completion_inject: mark_delivered failed: %s", e)


def inject_completions(session_id: str, messages: list[dict]) -> int:
    """Prepend background-task updates to the LAST user message.

    Returns how many completions were injected (and marked delivered).
    Call only on FRESH turns — approval continuations rebuild their
    messages from paused state and must not be mutated.
    """
    if not messages or messages[-1].get("role") != "user":
        return 0
    tasks = pending_completions(session_id)
    if not tasks:
        return 0

    lines = ["<system-reminder>Background task updates since your last turn:"]
    for t in tasks:
        lines.append(
            f'- task {t["task_id"]} "{(t.get("title") or "")[:80]}" finished: '
            f"{t['status']}. Summary: {_terminal_paragraph(t.get('result_text') or '')} "
            f'(full result: task_output("{t["task_id"]}"))'
        )
    lines.append("Factor these into your response.</system-reminder>")
    note = {"type": "text", "text": "\n".join(lines)}

    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [note, {"type": "text", "text": content}] if content else [note]
    elif isinstance(content, list):
        content.insert(0, note)
    else:
        return 0
    mark_delivered([t["task_id"] for t in tasks])
    return len(tasks)
