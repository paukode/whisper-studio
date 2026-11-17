"""One reminder when a chat turn starts to build with no question asked.

The prompt tells the model to shape a large piece of work before it starts
(server/prompts/base.py ASK_BEFORE_LONG_WORK). A prompt rule alone is soft, so
the engine backs it once a turn: when a turn has made a new file or card and
holds no question to the user, the tool results of that round carry this
reminder. Nothing is refused and no round is added. The
model reads it with the results and either asks or goes on.

The check reads the message list only, so it survives a pause and a resume
and keeps no state of its own. An edit to a file that exists is a change, not
a build, and does not count.
"""

ASK_TOOL = "ask_user_question"

# Calls that make a new deliverable: a file, a document, an artifact card.
BUILD_TOOLS = frozenset(
    {
        "ws_write_file",
        "ws_create_file",
        "save_file",
        "create_docx",
        "create_pptx",
        "create_xlsx",
        "create_pdf",
        "office_script",
        "create_artifact",
    }
)

REMINDER = (
    "<system-reminder>You started to build and asked the user nothing in this turn. Before you "
    "build more, look at what you assumed and at the limits of where the result is made or "
    "used. If an assumption would change the result, or a limit does not allow what the user "
    "asked for, ask now with ask_user_question: real options, your recommendation first. If "
    "nothing is open, the task is small, or the user's rules, your memory or an earlier answer "
    "settle it, go on and end your reply with the assumptions you made.</system-reminder>"
)


def _calls(message: dict) -> list[str]:
    content = message.get("content") if isinstance(message, dict) else None
    if message.get("role") != "assistant" or not isinstance(content, list):
        return []
    return [
        str(b.get("name") or "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


def _turn(messages: list) -> list | None:
    """The rows of the current turn, its prompt first, or None when an
    in-turn summary replaced the start of it (the turn is long under way and
    its earlier calls are gone, so nothing can be said about them)."""
    from server.chat.compaction import SUMMARY_HEADER
    from server.goals.deliverables import _is_user_prompt, _texts

    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and _is_user_prompt(messages[i]):
            texts = _texts(messages[i].get("content"))
            if any(SUMMARY_HEADER in t for t in texts):
                return None
            return [m for m in messages[i:] if isinstance(m, dict)]
    return None


def reminder_for(messages: list, tool_uses: list) -> str | None:
    """The reminder for this round, or None.

    ``messages`` ends with the round's assistant message and its tool
    results; ``tool_uses`` are that round's calls. The reminder is due when
    the turn holds a build call, no question to the user, and no reminder
    yet. The build call can be in an earlier round: a round that stopped
    for an approval card carries no reminder, so the next round does."""
    rows = _turn(messages)
    if rows is None:
        return None
    names = [name for row in rows for name in _calls(row)]
    names += [str(u.get("name") or "") for u in tool_uses if isinstance(u, dict)]
    if ASK_TOOL in names or not any(name in BUILD_TOOLS for name in names):
        return None
    from server.goals.deliverables import _texts

    if any(REMINDER in t for row in rows for t in _texts(row.get("content"))):
        return None
    return REMINDER
