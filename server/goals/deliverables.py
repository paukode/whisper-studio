"""Claimed-deliverable check for the completion gate.

The model's final reply routinely says "Saved to `/path/report.docx`" or "the
artifact card above" on its own belief. In two real sessions those were false:
a workflow's file was never written, and no artifact had been created, and the
user found out only by asking. This module reads the turn's replies (the
reply being gated, one a pending mid-turn message kept from the gate, the
first half of a max_tokens split), pulls out every file path they say was
made, and checks the file exists and is non-empty (workspace-relative paths
resolve against the workspace). An artifact-card claim is checked against a
``create_artifact`` call in the same turn. A miss becomes gate feedback, and
the turn continues until the file exists or the reply is corrected, at most
MAX_CLAIM_NUDGES times per turn.

Pure functions over the provider-neutral message list; no I/O beyond stat.
"""

from __future__ import annotations

import os
import re

from server.goals.tail import _render_blocks

# Extensions a reply plausibly hands the user as a finished artifact. Code and
# config files are deliberately absent: a reply mentioning `/src/app.py` is
# describing work, not claiming a deliverable.
_DELIVERABLE_EXTS = "docx|xlsx|pptx|pdf|html?|md|csv|json|txt|png|svg|zip|dmg"

# The app's own file link: [label](#wsfile=/abs/or/relative/path&open=os).
_WSFILE_RE = re.compile(r"#wsfile=([^&)\s\"']+)")
# An absolute or home-relative path with a deliverable extension, as it
# appears in prose or backticks. The lookbehind keeps it from matching the
# tail of a longer token; the trailing \b keeps "report.docx." clean.
_ABS_PATH_RE = re.compile(
    rf"(?<![\w/.])((?:~|/)[^\s`'\"()\[\]<>]*?\.(?:{_DELIVERABLE_EXTS}))\b", re.IGNORECASE
)
_ARTIFACT_CLAIM_RE = re.compile(
    r"\bartifact (?:card )?(?:above|below|attached)\b|\bin the artifact\b|\bartifact card\b",
    re.IGNORECASE,
)

# ── Reading a reply for claims ─────────────────────────────────────────────
# A path is a claim only where the reply says the file was made. Its clause
# has a completion word ("saved", "created", "is ready", "here"), or puts the
# path right after a location word ("to", "at", "in", "as", a colon); the
# app's own file link is a claim by itself. A clause that negates, speaks of
# what would happen, gives an example, or offers or plans in the first person
# ahead of the path is not a claim, and neither is a question or a code
# block. So "Saved to X, but the logo was not included" claims X, while "Want
# me to save it as X?", "I could not write X", "I will write X next", "2.
# write X" and "for example X" claim nothing, and a correction that names the
# missing path settles the check instead of repeating it. A list item is read
# with the line that introduces it ("I created these files:").
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_ABBREVIATIONS = {"e.g.": "for example", "i.e.": "that is"}
_ABBREV_RE = re.compile(r"\b(?:e\.g|i\.e)\.", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+|[;,]\s+|\s+[-\u2013]\s+"
    r"|\s+(?:but|though|although|however|so|then|while|whereas)\s+",
    re.IGNORECASE,
)
_DONE_RE = re.compile(
    r"\b(?:saved|wrote|written|created|exported|generated|produced|stored|placed|put"
    r"|rendered|built|converted|updated|downloaded|attached|ready|available|here|find"
    r"|located|lives)\b|\b(?:is|are) (?:now )?(?:at|in)\b",
    re.IGNORECASE,
)
_LOCATED_RE = re.compile(r"(?:\b(?:to|at|in|as|into|under)|:)[\s`*_\"'(\[]*$", re.IGNORECASE)
_LINK_OPEN_RE = re.compile(r"\[[^\]]*\]\(\s*$")
_NOT_MADE_RE = re.compile(
    r"\b(?:not|never|unable|failed|cannot|no longer|would|could|might|for example"
    r"|such as|example)\b|n['\u2019]t\b",
    re.IGNORECASE,
)
_OFFER_AHEAD_RE = re.compile(
    r"\b(?:i|we)(?:['\u2019](?:ll|d)|\s+(?:will|shall|may|am going to|are going to))\b"
    r"|\b(?:i|we)\s+can\b(?!\s+(?:confirm|see|tell|verify))"
    r"|\b(?:i|we)['\u2019](?:m|re)\s+going to\b"
    r"|\blet me\b(?!\s+know)"
    r"|\b(?:shall i|should i|want me to|like me to|if you|will be|going to be|no)\b",
    re.IGNORECASE,
)
_PATHS_ONLY_RE = re.compile(r"[^A-Za-z]+|\band\b|\bor\b", re.IGNORECASE)

# One nudge per turn, as for a requested file: claims are read out of prose,
# so a wrong reading (a reply about an artifact card an earlier turn made)
# costs a single round and never a loop to the cap.
MAX_CLAIM_NUDGES = 1
CLAIM_MARKER = "[claim]"
_GATE_PREFIX = "[completion gate]"
_MIDTURN_OPEN = "<user_message_mid_turn>"
_MIDTURN_CLOSE = "</user_message_mid_turn>"
_REMINDER_OPEN = "<system-reminder>"
_CONTINUE_PREFIX = "Continue exactly where you left off. Do not repeat anything."
# Rows the loop itself files under role=user inside a running turn: gate
# feedback, a mid-turn message or reminder written after an assistant tail
# (runner._remind), the max_tokens continuation (its own opening sentence, so
# a user who types "continue where you left off" still starts a turn). They
# continue the turn and never start one. A compaction summary is not listed:
# once it has replaced the prompt it is the best anchor left, and it quotes
# the user verbatim.
_ENGINE_PREFIXES = (_GATE_PREFIX, _MIDTURN_OPEN, _REMINDER_OPEN, _CONTINUE_PREFIX)
_TOOL_RESULT_TYPES = ("tool_result", "function_call_output")


def _is_user_prompt(m: dict) -> bool:
    """A real user turn, as opposed to tool results, gate feedback or other
    engine rows that the loop also files under role=user. A row of blocks is
    the user's when it carries no tool result and either holds an image or a
    document with no text, or has a text block of their own: the
    background-task note (agents/completion_inject.py) goes in front of the
    prompt as a block of its own, while an engine row holds engine text
    only."""
    if m.get("role") != "user":
        return False
    content = m.get("content")
    if isinstance(content, str):
        return not content.startswith(_ENGINE_PREFIXES)
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if not blocks or any(b.get("type") in _TOOL_RESULT_TYPES for b in blocks):
            return False
        texts = _texts(blocks)
        if not texts:
            return True
        return any(not t.startswith(_ENGINE_PREFIXES) for t in texts)
    return False


def _texts(content) -> list[str]:
    """The text of each text block of one message (a string is one block)."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        str(b.get("text", ""))
        for b in content
        if isinstance(b, dict) and b.get("type") in ("text", "input_text")
    ]


def _midturn_words(text: str) -> str:
    """The user's own words in a mid-turn message, without the wrapper
    runner._midturn_text puts around them (a note to the model, then a blank
    line); "" for any other text."""
    if not text.startswith(_MIDTURN_OPEN):
        return ""
    inner = text[len(_MIDTURN_OPEN) :].removesuffix(_MIDTURN_CLOSE)
    _note, sep, words = inner.partition("\n\n")
    return (words if sep else inner).strip()


def turn_messages(messages: list) -> list:
    """Messages since the last real user prompt (the current turn)."""
    for i in range(len(messages) - 1, -1, -1):
        if _is_user_prompt(messages[i]):
            return messages[i + 1 :]
    return list(messages)


def last_user_prompt(messages: list) -> str:
    """What the user asked for this turn: the real prompt it is answering,
    then the words of each message the user sent while it ran, whether it was
    folded onto the prompt (loop_hints.inject_reminder) or onto a later row (a
    tool result, or a row of its own after an assistant tail). Gate feedback,
    tool results and engine reminders, agent reports among them, are never
    part of it."""
    for i in range(len(messages) - 1, -1, -1):
        if _is_user_prompt(messages[i]):
            break
    else:
        return ""
    asked = [
        _midturn_words(t) or t
        for t in _texts(messages[i].get("content"))
        if not t.startswith(_REMINDER_OPEN)
    ]
    asked += [
        words
        for m in messages[i + 1 :]
        if isinstance(m, dict) and m.get("role") == "user"
        for t in _texts(m.get("content"))
        if (words := _midturn_words(t))
    ]
    return "\n\n".join(t for t in asked if t)


def _calls_a_tool(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in ("tool_use", "function_call") for b in content
    )


def reply_texts(messages: list) -> list[str]:
    """The text of each reply of this turn, oldest first: the assistant rows
    that call no tool, and the last assistant row (the reply being gated)
    whatever it holds. A reply a pending mid-turn message kept from the gate
    is one of them, and the two halves of a max_tokens split are joined into
    one, as the chat shows them."""
    rows: list[str] = []
    joins_next = False
    turn = [m for m in turn_messages(messages) if isinstance(m, dict)]
    last = max((i for i, m in enumerate(turn) if m.get("role") == "assistant"), default=-1)
    for i, m in enumerate(turn):
        if m.get("role") == "user":
            joins_next = joins_next and any(
                t.startswith(_CONTINUE_PREFIX) for t in _texts(m.get("content"))
            )
            continue
        if m.get("role") != "assistant" or (i != last and _calls_a_tool(m.get("content"))):
            joins_next = False
            continue
        text = _render_blocks(m.get("content"))
        if joins_next and rows:
            rows[-1] += text
        elif text:
            rows.append(text)
        joins_next = True
    return [t for t in rows if t]


def claimed_paths(text: str) -> list[str]:
    """File paths the text names as deliverables, in order, deduplicated."""
    found: list[str] = []
    for m in _WSFILE_RE.finditer(text or ""):
        found.append(m.group(1))
    for m in _ABS_PATH_RE.finditer(text or ""):
        found.append(m.group(1))
    seen: set[str] = set()
    out: list[str] = []
    for p in found:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _clauses(text: str):
    """(lead, clause) for each clause of each line outside code blocks; the
    lead is the line that introduces a list, for the list's items."""
    text = _CODE_FENCE_RE.sub("\n", text or "")
    text = _ABBREV_RE.sub(lambda m: _ABBREVIATIONS[m.group(0).lower()], text)
    lead = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        item = bool(_LIST_ITEM_RE.match(line))
        if not item:
            lead = line if line.rstrip().endswith(":") else ""
        for clause in _CLAUSE_SPLIT_RE.split(line):
            if clause and clause.strip():
                yield (lead if item else ""), clause


def _is_question(clause: str) -> bool:
    return clause.rstrip(" \t\"')]*_`").endswith("?")


def asserted_paths(text: str) -> list[str]:
    """The paths ``text`` says were made (see the notes above
    _CODE_FENCE_RE); claimed_paths less those it only offers, plans, supposes,
    negates or asks about."""
    out: list[str] = []
    prev_claimed = False
    for lead, clause in _clauses(text):
        paths = claimed_paths(clause)
        if not paths:
            prev_claimed = False
            continue
        # "Saved to X, Y and Z": a clause of paths alone continues the last.
        paths_only = not _PATHS_ONLY_RE.sub("", _ABS_PATH_RE.sub("", clause)).strip()
        claimed_here = False
        if not _is_question(clause) and not _NOT_MADE_RE.search(f"{lead} {clause}"):
            for p in paths:
                at = max(clause.find(p), 0)
                ahead = clause[:at]
                if _OFFER_AHEAD_RE.search(f"{lead} {ahead}"):
                    continue
                link = ahead.endswith("#wsfile=")
                ahead = _LINK_OPEN_RE.sub("", ahead.removesuffix("#wsfile="))
                if (
                    link
                    or _DONE_RE.search(f"{lead} {clause}")
                    or _LOCATED_RE.search(ahead)
                    or (paths_only and prev_claimed)
                ):
                    claimed_here = True
                    if p not in out:
                        out.append(p)
        prev_claimed = claimed_here
    return out


def claims_an_artifact(text: str) -> bool:
    """True when ``text`` points at an artifact card as made, as opposed to
    offering one, asking about one or saying there is none."""
    for lead, clause in _clauses(text):
        m = _ARTIFACT_CLAIM_RE.search(clause)
        if (
            m
            and not _is_question(clause)
            and not _NOT_MADE_RE.search(f"{lead} {clause}")
            and not _OFFER_AHEAD_RE.search(f"{lead} {clause[: m.start()]}")
        ):
            return True
    return False


def exists_non_empty(path: str, workspace: str | None) -> bool:
    """True when the path resolves to a file with content (workspace-relative
    paths resolve against the workspace)."""
    resolved = os.path.expanduser(path)
    if not os.path.isabs(resolved) and workspace:
        resolved = os.path.join(workspace, resolved)
    try:
        return os.path.isfile(resolved) and os.path.getsize(resolved) > 0
    except OSError:
        return False


def artifact_created(messages: list) -> bool:
    """True if this turn contains a create_artifact or edit_artifact call
    (Anthropic tool_use block or Responses function_call item); both put a
    card in the chat."""
    for m in turn_messages(messages):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if (
                isinstance(b, dict)
                and b.get("type") in ("tool_use", "function_call")
                and b.get("name") in ("create_artifact", "edit_artifact")
            ):
                return True
    return False


def claim_nudges_used(messages: list) -> int:
    """How many claim nudges the gate already issued this turn."""
    return sum(
        1
        for m in turn_messages(messages)
        if isinstance(m, dict)
        and m.get("role") == "user"
        and any(t.startswith(f"{_GATE_PREFIX} {CLAIM_MARKER}") for t in _texts(m.get("content")))
    )


def check_claims(
    messages: list,
    workspace: str | None,
    *,
    plan_mode: bool = False,
    max_attempts: int = MAX_CLAIM_NUDGES,
) -> str | None:
    """Gate feedback naming what this turn's replies say was made but was
    not, or None when every claim checks out. Every reply of the turn is read,
    so one the gate never judged (a pending mid-turn message, or a Stop hook
    that blocked first) is checked too; a reply that passed passes again.

    Plan mode refuses the workspace writes, so a path there is the file the
    plan will write, and only an artifact-card claim is checked."""
    if claim_nudges_used(messages) >= max_attempts:
        return None
    text = "\n".join(reply_texts(messages))
    if not text:
        return None
    paths = [] if plan_mode else asserted_paths(text)
    missing = [p for p in paths if not exists_non_empty(p, workspace)]
    artifact_missing = claims_an_artifact(text) and not artifact_created(messages)
    if not missing and not artifact_missing:
        return None
    parts: list[str] = []
    if missing:
        shown = ", ".join(missing[:5]) + (
            f" and {len(missing) - 5} more" if len(missing) > 5 else ""
        )
        parts.append(
            f"your reply tells the user these files were saved, but they do not exist "
            f"or are empty: {shown}"
        )
    if artifact_missing:
        parts.append(
            "your reply refers to an artifact card, but no create_artifact call happened this turn"
        )
    return (
        f"{CLAIM_MARKER} "
        + "; ".join(parts)
        + ". Produce it now and confirm from the tool result, or correct your reply to say it "
        "was not produced. Never report a deliverable you have not verified."
    )
