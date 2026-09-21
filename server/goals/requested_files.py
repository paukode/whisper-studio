"""Requested-deliverable check for the completion gate.

``deliverables.py`` catches the reply that CLAIMS a file it never wrote. This
module catches the opposite miss, which cost a real session: the user asked
for a diagram and a config file to be updated and saved to Downloads, the turn
answered in chat with a revised description, wrote nothing, and ended. Asked
afterwards, the model was honest ("no revised file was created, I
misunderstood your request as a text-only correction"), but the user only
found out by asking.

So: when this turn's user prompt asks for a file and the turn produced none,
the gate says so once and the model either produces it or states plainly that
it will not. One nudge per turn, because the detection is a heuristic over
prose and a wrong guess must cost at most one round.

Pure functions over the provider-neutral message list; no I/O beyond stat.
"""

from __future__ import annotations

import json
import re

from server.goals.deliverables import (
    claimed_paths,
    exists_non_empty,
    last_assistant_text,
    last_user_prompt,
    turn_messages,
)

# One nudge per turn. The ask is read out of prose, so a false positive costs
# a single round and never a loop.
MAX_REQUEST_NUDGES = 1
REQUEST_MARKER = "[file]"

# Tools that put a real file (or an artifact card, which the user can save)
# in front of the user. A turn that called any of them produced something.
_FILE_TOOLS = frozenset(
    {
        "save_file",
        "ws_write_file",
        "ws_create_file",
        "ws_edit_file",
        "create_artifact",
        "edit_artifact",
        "create_visual",
        "create_chart",
        "create_program",
        "notebook_edit",
    }
)

_FILE_EXTS = (
    r"docx?|xlsx?|pptx?|pdf|csv|tsv|json|ya?ml|md|markdown|html?|txt"
    r"|png|jpe?g|svg|gif|webp|zip|dmg|ics|ipynb"
)
# ".png", "gitlab-ci.yml": an extension is the least ambiguous way to ask.
_EXT_RE = re.compile(rf"\.(?:{_FILE_EXTS})\b", re.IGNORECASE)

# The verb has to be about producing or handing something over. "read",
# "check", "explain", "summarize" are deliberately absent: those are about a
# file that already exists, and nothing is owed back.
_PRODUCE_RE = re.compile(
    r"\b(?:creat(?:e|ing)|generat(?:e|ing)|mak(?:e|ing)|produc(?:e|ing)|build|draw"
    r"|render|writ(?:e|ing)|sav(?:e|ing)|stor(?:e|ing)|export(?:ing)?|output"
    r"|updat(?:e|ing)|revis(?:e|ing)|regenerat(?:e|ing)|re-?do|attach|download"
    r"|give me|send me|hand me|provide)\b",
    re.IGNORECASE,
)

_FILE_NOUN_RE = re.compile(
    r"\b(?:files?|documents?|reports?|spreadsheets?|workbooks?|decks?|slides"
    r"|presentations?|diagrams?|flowcharts?|charts?|graphs?|images?|pictures?"
    r"|screenshots?|visuals?|copy|version|export|attachments?|artifacts?"
    r"|png|jpe?g|svg|pdf|docx?|xlsx?|pptx?|csv|tsv|ya?ml|json|markdown|zip)\b",
    re.IGNORECASE,
)

# Sentence-ish split: the verb and the noun have to be in the same breath, so
# "read the pdf, then write a summary here" does not read as a file request.
# A full stop only ends a sentence when whitespace or the end follows it,
# otherwise "update gitlab-ci.yml" splits down the middle of the filename.
_CLAUSE_SPLIT_RE = re.compile(r"[\n;]+|[.!?]+(?=\s|$)|\bthen\b|\bafter that\b", re.IGNORECASE)

# Asking ABOUT a file is not asking FOR one. "did you update the png", "what
# file did you store it in" are the user checking on work already done, and
# reading those as a request turns a plain answer into a pointless round.
# "can"/"could"/"will you" are deliberately absent: those are requests.
_ASKING_ABOUT_RE = re.compile(
    r"^(?:and|so|ok|okay|but|also)?[,\s]*"
    r"(?:did|do|does|is|are|was|were|has|have|had|what|where|which|why|when|who)\b"
    r"|\b(?:did|have|has|had|was|were)\s+(?:you|it|they|we|i)\b",
    re.IGNORECASE,
)


def _tool_input(block: dict) -> dict:
    inp = block.get("input") or block.get("arguments") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except (TypeError, ValueError):
            inp = {}
    return inp if isinstance(inp, dict) else {}


def requested_clause(prompt: str) -> str | None:
    """The clause where the user asks for a file, or None.

    A clause qualifies on a producing verb plus either a file extension or a
    file-shaped noun. Both have to sit in the same clause."""
    for clause in _CLAUSE_SPLIT_RE.split(prompt or ""):
        clause = clause.strip()
        if not clause or not _PRODUCE_RE.search(clause):
            continue
        if _ASKING_ABOUT_RE.search(clause):
            continue
        if _EXT_RE.search(clause) or _FILE_NOUN_RE.search(clause):
            return " ".join(clause.split())[:160]
    return None


def produced_a_file(messages: list, workspace: str | None) -> bool:
    """True when this turn actually put a file (or artifact) in front of the
    user: a file tool ran, or the reply names a path that really exists.

    The second arm matters for the skill path, where a document is written by
    a script rather than by a file tool."""
    for m in turn_messages(messages):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") in ("tool_use", "function_call") and b.get("name") in _FILE_TOOLS:
                return True
    return any(exists_non_empty(p, workspace) for p in claimed_paths(last_assistant_text(messages)))


def request_nudges_used(messages: list) -> int:
    """How many file nudges the gate already issued this turn."""
    n = 0
    for m in turn_messages(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        text = content if isinstance(content, str) else ""
        if isinstance(content, list):
            text = " ".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("text")
            )
        if text.startswith(f"[completion gate] {REQUEST_MARKER}"):
            n += 1
    return n


def requested_file_feedback(
    messages: list,
    workspace: str | None,
    *,
    plan_mode: bool = False,
    max_attempts: int = MAX_REQUEST_NUDGES,
) -> str | None:
    """Gate feedback when the user asked for a file this turn and none was
    produced, or None when there is nothing to ask for.

    Silent in plan mode: writing is refused there by design, so the turn is
    supposed to end with a plan and no file."""
    if plan_mode:
        return None
    clause = requested_clause(last_user_prompt(messages))
    if not clause:
        return None
    if produced_a_file(messages, workspace):
        return None
    if request_nudges_used(messages) >= max_attempts:
        return None
    return (
        f'{REQUEST_MARKER} the user asked you for a file ("{clause}") and this turn has not '
        "produced one: no file tool ran and your reply names no file that exists. Write it now "
        "(save_file for a path the user named such as ~/Downloads, the workspace file tools for "
        "the connected workspace), confirm the path from the tool result, and give that path in "
        "your reply. If you are not going to produce it, say so plainly and why, in the reply "
        "itself, rather than answering in chat as though no file was asked for."
    )


__all__ = [
    "MAX_REQUEST_NUDGES",
    "REQUEST_MARKER",
    "produced_a_file",
    "request_nudges_used",
    "requested_clause",
    "requested_file_feedback",
]
