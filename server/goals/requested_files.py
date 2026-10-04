"""Requested-deliverable check for the completion gate.

``deliverables.py`` catches the reply that CLAIMS a file it never wrote. This
module catches the opposite miss, which cost a real session: the user asked
for a diagram and a config file to be updated and saved to Downloads, the turn
answered in chat with a revised description, wrote nothing, and ended. Asked
afterwards, the model was honest ("no revised file was created, I
misunderstood your request as a text-only correction"), but the user only
found out by asking.

So: when the user asks for a file this turn and the turn produced none, the
gate says so once and the model either produces it or states plainly that it
will not. Each request is checked on its own: one in the prompt against the
whole turn, and one in a message the user sent while the turn ran ("also make
a PNG chart") against only what happened after that message, since a file
made before it cannot be the one it asks for. One nudge per turn, naming what
is still owed, because the detection is a heuristic over prose and a wrong
guess must cost at most one round.

Only an explicit ask counts. What the user asks for goes in the reply unless
they ask for a file, so a summary, report, plan or document is no file
request, and neither are the transcript, attachments and inlined mentions
that share the prompt's message (the gate reads what the user typed). Read
that way, "summarize this" once came back as a saved .txt and a .docx.

Pure functions over the provider-neutral message list; no I/O beyond stat.
"""

from __future__ import annotations

import re

from server.goals.claims import asserted_paths
from server.goals.deliverables import (
    OUTSIDE_WRITERS,
    asked_by_row,
    assistant_named_paths,
    called_tools,
    exists_non_empty,
    reply_texts,
    resolve_path,
    turn_messages,
)

# One nudge per turn. The ask is read out of prose, so a false positive costs
# a single round and never a loop.
MAX_REQUEST_NUDGES = 1
REQUEST_MARKER = "[file]"

# Tools that write a real file to disk: the file tools, and the document
# tools, which build the document and save it behind their approval card.
_DISK_TOOLS = frozenset(
    {
        "save_file",
        "ws_write_file",
        "ws_create_file",
        "ws_edit_file",
        "notebook_edit",
        "create_docx",
        "create_pptx",
        "create_xlsx",
        "create_pdf",
        "office_script",
    }
)
# Tools that may write a file again under a name the turn used before: the
# ones that write outside the workspace, the workspace command runner (this
# check never runs in plan mode, where it is refused), and a skill, whose
# agent writes with tools of its own.
_REMAKERS = OUTSIDE_WRITERS | {"ws_run_command", "skill_invoke"}
# Plus the ones that hand the user something in the chat itself. Enough for
# "make me a diagram", not for "save it to Downloads".
_FILE_TOOLS = _DISK_TOOLS | {
    "create_artifact",
    "edit_artifact",
    "create_visual",
    "create_chart",
    "create_program",
}

# The user named somewhere on disk, so only a written file will do.
_LOCATION_RE = re.compile(
    r"\b(?:downloads?|desktop|documents folder|on disk|to disk|locally"
    r"|as a file|to a file|in a file)\b|~/|(?<![\w.])/(?:Users|home|tmp|var)/",
    re.IGNORECASE,
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
# Saving something somewhere asks for a file with or without a file word ("a
# version that can be saved locally"). Not "saved": "I saved it in Downloads"
# tells where a file already is.
_SAVE_RE = re.compile(r"\b(?:sav(?:e|ing)|stor(?:e|ing)|be (?:saved|stored))\b", re.IGNORECASE)

# A file by name: only a written one will do.
_DISK_NOUN_RE = re.compile(
    r"\b(?:files?|pdf|docx?|xlsx?|pptx?|zip|exports?|attachments?)\b",
    re.IGNORECASE,
)
# Something to look at, which the chat can show as well as a file can.
_SHOW_NOUN_RE = re.compile(
    r"\b(?:diagrams?|flowcharts?|charts?|graphs?|images?|pictures?|screenshots?"
    r"|visuals?|artifacts?|decks?|slides|presentations?|spreadsheets?|workbooks?"
    r"|png|jpe?g|svg)\b",
    re.IGNORECASE,
)
# Deliberately absent: report, document, summary, notes, copy, version,
# markdown, json, csv. Text the user asks for belongs in the reply unless they
# ask for a file, which the words above, a file name or a place on disk say.

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


# Calling a file off is not asking for one: "no need to save a file", "don't
# write the pdf", "stop exporting it". A producing verb counts only where
# none of these sits in the two words before it ("don't forget to save it"
# still asks), so "don't save anything, just make me a diagram" asks for the
# diagram.
_CALLED_OFF_RE = re.compile(
    r"\b(?:don['\u2019]?t|do not|no need to|never|not|without|stop)\s+"
    r"(?:(?!forget\b)\w+\s+){0,2}$",
    re.IGNORECASE,
)


def _live(verbs: re.Pattern, clause: str) -> bool:
    """A verb of ``verbs`` in the clause that the user did not call off."""
    return any(not _CALLED_OFF_RE.search(clause[: m.start()]) for m in verbs.finditer(clause))


def requested_clauses(text: str) -> list[str]:
    """Each clause where the user asks for a file, in order.

    A clause qualifies on a producing verb the user did not call off, plus a
    file extension or a file-shaped noun (``_DISK_NOUN_RE``,
    ``_SHOW_NOUN_RE``), or on saving something to a place (``_SAVE_RE``,
    ``_LOCATION_RE``). Each pair has to sit in the same clause."""
    found: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(text or ""):
        clause = clause.strip()
        if not clause or _ASKING_ABOUT_RE.search(clause):
            continue
        named = (
            _EXT_RE.search(clause) or _DISK_NOUN_RE.search(clause) or _SHOW_NOUN_RE.search(clause)
        )
        if (named and _live(_PRODUCE_RE, clause)) or (
            _LOCATION_RE.search(clause) and _live(_SAVE_RE, clause)
        ):
            found.append(" ".join(clause.split())[:160])
    return found


def wants_disk(clause: str) -> bool:
    """True when only a file on disk answers the clause: it names a place, a
    file name, or a file (``_DISK_NOUN_RE``). A visual or a deck asked for
    without one is answered in the chat too."""
    return bool(
        _LOCATION_RE.search(clause) or _EXT_RE.search(clause) or _DISK_NOUN_RE.search(clause)
    )


def file_requests(messages: list, asked: str | None = None) -> list[tuple[str, int]]:
    """Each file the user asked for this turn, with the row of the turn
    (``turn_messages`` order) from which what the turn did can answer it: 0,
    the whole turn, for the prompt and for words folded onto the prompt row;
    the row after it for a message the user sent while the turn ran, folded
    onto a tool result or given a row of its own. Nothing that came before a
    message can be the file it asks for (``produced_a_file``). ``asked`` is
    what the user typed, read in place of the prompt row (``asked_by_row``)."""
    return [
        (clause, row + 1)
        for row, words in asked_by_row(messages, asked)
        for clause in requested_clauses(words)
    ]


def produced_a_file(
    messages: list, workspace: str | None, *, on_disk: bool = False, since: int = 0
) -> bool:
    """True when this turn actually put a file (or artifact) in front of the
    user from row ``since`` of the turn on: a file tool ran, or one of its
    replies names a path that really exists.

    The second arm matters for the skill path, where a document is written by
    a script rather than by a file tool. Every reply counts, so a later reply
    that answers a mid-turn question without naming the file again does not
    undo it, but only a path a reply says it made: an input file it read is
    not the deliverable. From a later row, a path the assistant named before
    it is the file made for an earlier ask ("Saved the report to X" restated
    in the reply that answers "also make a PNG chart"), unless a tool that may
    write it again ran since (_REMAKERS). A path the user gave is theirs to
    name: the file made there answers the request. With ``on_disk`` an
    artifact card does not count: the user asked for a file somewhere they
    can open it."""
    accepted = _DISK_TOOLS if on_disk else _FILE_TOOLS
    turn = turn_messages(messages)
    ran = called_tools(turn[since:])
    if any(name in accepted for name in ran):
        return True
    earlier: set[str] = set()
    if since and not any(name in _REMAKERS for name in ran):
        earlier = assistant_named_paths(turn[:since], workspace)
    return any(
        exists_non_empty(p, workspace) and resolve_path(p, workspace) not in earlier
        for text in reply_texts(messages, since=since)
        for p in asserted_paths(text)
    )


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
    asked: str | None = None,
) -> str | None:
    """Gate feedback when the user asked for a file this turn and none was
    produced, or None when there is nothing to ask for. Each request is
    checked against what came after it (``file_requests``), and the feedback
    names every one still owed. ``asked`` is what the user typed this turn.

    Silent in plan mode: the workspace writes are refused there by design,
    so the turn is supposed to end with a plan and no file."""
    if plan_mode or request_nudges_used(messages) >= max_attempts:
        return None
    owed: list[str] = []
    for clause, since in file_requests(messages, asked):
        if clause not in owed and not produced_a_file(
            messages, workspace, on_disk=wants_disk(clause), since=since
        ):
            owed.append(clause)
    if not owed:
        return None
    listed = "; ".join(f'"{c}"' for c in owed[:3])
    if len(owed) > 3:
        listed += f" and {len(owed) - 3} more"
    what, it = ("a file", "it") if len(owed) == 1 else ("files", "them")
    how = []
    if any(wants_disk(c) for c in owed):
        how.append(
            "For a file: write it now (save_file, to the place the user named if they named "
            "one, or the workspace file tools in the connected workspace), confirm the path "
            "from the tool result, and give that path in your reply."
        )
    if not all(wants_disk(c) for c in owed):
        how.append(
            "For something to look at: show it in the chat now (create_visual or create_chart "
            "for a diagram or chart, create_artifact for a page, a deck or a table), and do not "
            "save it as a file unless the user asked for one."
        )
    return (
        f"{REQUEST_MARKER} the user asked you for {what} ({listed}) and this turn has not "
        f"produced {it}: nothing that makes {it} ran after the request and no reply since "
        f"names a file that exists. {' '.join(how)} If you are not going to produce {it}, say "
        "so plainly and why, in the reply itself, rather than answering as though nothing was "
        "asked for."
    )


__all__ = [
    "MAX_REQUEST_NUDGES",
    "REQUEST_MARKER",
    "file_requests",
    "produced_a_file",
    "request_nudges_used",
    "requested_clauses",
    "requested_file_feedback",
    "wants_disk",
]
