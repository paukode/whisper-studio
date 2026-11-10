"""The message list of a turn that resumes a paused one.

A turn pauses when a round's tool batch holds an approval-gated call, a
question card or the workspace folder card: the engine stashes the canonical
messages plus a placeholder tool_result for each call (server/chat/engine/
pause.py). Two requests resume it:

- the answer to a card (``approved_tool_result``): resume_messages;
- a new message the user sent instead of answering (``skipped_cards``):
  resume_with_message. The cards it skips are closed (an approval or folder
  call did not run, a question was not answered), and the message joins the
  same turn, so the model knows what the user skipped and what they asked
  instead. Before this, the new message started a separate turn over the
  client's history, which holds no tool calls: the model never learned that a
  call waited, and the card stayed on screen answering nothing.
"""

from __future__ import annotations

# The tool_result of an approval or folder card the user did not answer.
CARD_SKIPPED = (
    "[Not run] The user did not answer this card and sent a new message instead, "
    "so this call did not run."
)


def resume_messages(messages: list, answers: list[dict], paused: dict | None) -> list:
    """The message list for a continuation turn (approval decision / answer).

    With paused state: restore the stashed messages, which already carry the
    assistant message holding every tool_use block, and fill the pre-computed
    placeholder tool_results in by tool_use_id, so every tool_use is answered.
    Bedrock rejects the whole request otherwise. An id with no placeholder is
    appended (the tool that triggered the pause may not have one).

    WITHOUT paused state (the backend restarted while the card sat on screen):
    the assistant message carrying the tool_use is gone with it, so a
    tool_result block here would be an orphan: sanitize_tool_pairs strips it
    and the model would answer a turn with neither the tool call nor its
    outcome in context, which reads as a confident non-answer rather than an
    error. Replay the outcome as plain text instead: well-formed for every
    provider, and the client-rebuilt `history` in ``messages`` gives it the
    conversation it belongs to.
    """
    if paused:
        from server.tool_executor import ASK_PAUSE_MARKER, ASK_UNANSWERED

        blocks = list(paused["pending_tool_results"])
        for ans in answers:
            tool_use_id = ans.get("tool_use_id", "")
            replaced = False
            for block in blocks:
                if block.get("tool_use_id") == tool_use_id:
                    block["content"] = ans.get("content", "")
                    replaced = True
                    break
            if not replaced:
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": ans.get("content", ""),
                    }
                )
        # A question the continuation brought no answer for must not keep a
        # marker that promises one. The model is told plainly and may ask again.
        for block in blocks:
            if block.get("content") == ASK_PAUSE_MARKER:
                block["content"] = ASK_UNANSWERED
        return [*paused["messages"], {"role": "user", "content": blocks}]

    outcomes = "\n\n".join(str(a.get("content", "")) for a in answers if a.get("content"))
    return [
        *messages,
        {
            "role": "user",
            "content": (
                "[The tool call you were waiting on is no longer in context: the "
                "app restarted while it awaited the user's decision. What the user "
                "decided:]\n\n"
                f"{outcomes}\n\n"
                "Continue from here. Re-issue that call only if the outcome above "
                "says it did not happen."
            ),
        },
    ]


def _blocks(content) -> list:
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": str(content)}] if content else []


def resume_with_message(messages: list, skipped: list[str], paused: dict | None) -> list:
    """The paused turn, with the cards in ``skipped`` closed and the new user
    message (the last entry of ``messages``, already built with its
    attachments, images and grounding) joined to the same user turn.

    ``messages`` is the fresh turn's list: the client's history plus that new
    message. With paused state the stashed messages replace the history; a
    question keeps its marker, which resume_messages turns into "not
    answered". Without it (a backend restart) the skipped calls are replayed
    as text over the history, as for any answer.
    """
    from server.tool_executor import ASK_PAUSE_MARKER

    *history, new_message = messages
    if paused:
        markers = {
            b.get("tool_use_id")
            for b in paused["pending_tool_results"]
            if b.get("content") == ASK_PAUSE_MARKER
        }
        answers = [{"tool_use_id": i, "content": CARD_SKIPPED} for i in skipped if i not in markers]
    else:
        answers = [{"tool_use_id": i, "content": CARD_SKIPPED} for i in skipped]
    resumed = resume_messages(history, answers, paused)
    last = resumed[-1]
    resumed[-1] = {**last, "content": _blocks(last["content"]) + _blocks(new_message["content"])}
    return resumed
