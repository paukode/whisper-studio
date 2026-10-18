"""Remove the background-task rows the chat no longer renders.

Every background task used to leave a task_event row in chat_history when it
started and when it finished, so the chat could show a card for each. The
header's task list now shows that lifecycle from the task registry alone and
the chat carries only the model's words, so the rows are dead weight in every
session that ever ran a background command. Sessions whose history holds none
are not rewritten; a history that does not parse is left alone.
"""

import json
import sqlite3

VERSION = 24
DESCRIPTION = "sessions: drop task_event rows from chat_history"


def migrate(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, chat_history FROM sessions WHERE chat_history LIKE '%task_event%'"
    ).fetchall()
    for session_id, raw in rows:
        try:
            history = json.loads(raw or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(history, list):
            continue
        kept = [m for m in history if not (isinstance(m, dict) and m.get("role") == "task_event")]
        if len(kept) != len(history):
            conn.execute(
                "UPDATE sessions SET chat_history=? WHERE id=?",
                (json.dumps(kept), session_id),
            )
