"""The whole text of each long tool result, kept per session for the export.

The chat shows, and the session row saves, a preview of each tool result
(server/tool_executor.py PREVIEW_CHARS). That keeps a long session light in
the window and in every save, but an export made from the saved row then held
only the previews, which was too little to diagnose a session from. So the
text the model received for a call is kept here whenever the preview cut it,
and the export puts it back (server/infrastructure/sessions_routes.py).

One file per session, ``<data root>/tool_results/<session_id>.jsonl``, one
``{"id": tool_use_id, "text": ...}`` line per call. A result is at most the
tool result budget (server/chat/budget.py) long. Deleting a session deletes
its file. Never raises: a record that cannot be written costs the export that
one full text, never the turn.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

_lock = threading.Lock()


def _file(session_id: str | None) -> str | None:
    """The session's file, or None when the id cannot name one (it arrives in
    request bodies, so it must be one plain file name)."""
    safe = (session_id or "").strip()
    if not safe or any(c in safe for c in ("/", "\\", "\0")) or safe[0] in "._":
        return None
    return os.path.join(data_root(), "tool_results", f"{safe}.jsonl")


def record(session_id: str | None, tool_use_id: str, text: str) -> None:
    """Keep ``text``, the result the model received for ``tool_use_id``."""
    path = _file(session_id)
    if not path or not tool_use_id:
        return
    try:
        line = json.dumps({"id": tool_use_id, "text": text}, ensure_ascii=False)
        with _lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except (OSError, TypeError, ValueError) as e:
        log.warning("tool result of %s not kept for the export (%s)", tool_use_id, e)


def load(session_id: str | None) -> dict[str, str]:
    """Every kept text of the session by tool_use_id; the last one wins for a
    call that was answered twice (an approval, then its real result)."""
    path = _file(session_id)
    kept: dict[str, str] = {}
    if not path:
        return kept
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a line cut by a crash mid-write
                if isinstance(row, dict) and isinstance(row.get("text"), str):
                    kept[str(row.get("id") or "")] = row["text"]
    except OSError:
        return kept
    kept.pop("", None)
    return kept


def forget(session_id: str | None) -> None:
    """Delete the session's kept results (the session was deleted)."""
    path = _file(session_id)
    if not path:
        return
    try:
        with _lock:
            os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("kept tool results of %s not deleted (%s)", session_id, e)
