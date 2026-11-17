"""How the user treats question cards, so the ask-first reminder follows it.

The reminder (server/chat/ask_first.py) is for a user who wants a say before
a large build. A user who leaves question cards without an answer does not:
after ``SKIPS_TO_QUIET`` skipped cards in a row the reminder goes quiet, and
one answered card turns it back on. The count is one number for the whole
app (a single user), kept in data_root()/ask_habit.json.

A card is skipped when the user pressed Skip on every question of it, or
sent a new message and left it open. A stepper of several questions is one
card. Approval cards do not count. What the user says about questions
("you decide", "you should have asked") is not counted here: the memory
review saves that as a note (server/memory/prompts.py).
"""

import json
import logging
import os

from server.ask_user import SKIPPED_ANSWER_PREFIX
from server.chat.ask_first import ASK_TOOL
from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

# Chosen by the user: one or two skips can be a rush, three is a habit.
SKIPS_TO_QUIET = 3


def _path() -> str:
    return os.path.join(data_root(), "ask_habit.json")


def skips_in_a_row() -> int:
    try:
        with open(_path(), encoding="utf-8") as f:
            return max(0, int(json.load(f).get("skipped_in_a_row", 0)))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _save(count: int) -> None:
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"skipped_in_a_row": count}, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("ask habit not saved: %s", e)


def quiet() -> bool:
    """True when the user skipped enough cards in a row to stop the reminder."""
    return skips_in_a_row() >= SKIPS_TO_QUIET


def _question_ids(messages: list) -> set[str]:
    ids = set()
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list) or m.get("role") != "assistant":
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == ASK_TOOL:
                ids.add(str(b.get("id") or ""))
    return ids


def note_answers(messages: list, answers: list) -> None:
    """Record the card these answers close. It counts as skipped when the
    user pressed Skip on every question of it."""
    questions = _question_ids(messages)
    given = [
        str(a.get("content") or "")
        for a in answers or []
        if isinstance(a, dict) and str(a.get("tool_use_id") or "") in questions
    ]
    if given:
        answered = not all(text.startswith(SKIPPED_ANSWER_PREFIX) for text in given)
        note_cards(messages, [a.get("tool_use_id") for a in answers], answered=answered)


def note_cards(messages: list, card_ids: list, *, answered: bool) -> None:
    """Record one closed card. ``card_ids`` are the tool_use ids the user
    answered or left for a new message; only a card that holds a question
    counts. ``messages`` is the history that holds those calls."""
    if not _question_ids(messages) & {str(i) for i in card_ids or []}:
        return
    count = 0 if answered else skips_in_a_row() + 1
    if count != skips_in_a_row():
        _save(count)
