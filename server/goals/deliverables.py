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
from typing import NamedTuple
from urllib.parse import unquote

from server.goals.tail import _render_blocks

# Extensions a reply plausibly hands the user as a finished artifact. Code and
# config files are deliberately absent: a reply mentioning `/src/app.py` is
# describing work, not claiming a deliverable.
_DELIVERABLE_EXTS = "docx|xlsx|pptx|pdf|html?|md|csv|json|txt|png|svg|zip|dmg"
_EXT = rf"\.(?:{_DELIVERABLE_EXTS})"
_EXT_RE = re.compile(rf"{_EXT}\b", re.IGNORECASE)

# The app's own file link: [label](#wsfile=/abs/or/relative/path&open=os). Its
# builder url-quotes the path (server/index/citations.py), so a space arrives
# as %20 and is decoded, as the chat's own link handler decodes it.
_WSFILE = "#wsfile="
_WSFILE_RE = re.compile(r"#wsfile=([^&)\s\"']+)")
# An absolute or home-relative path with a deliverable extension, as it
# appears in prose or backticks. The lookbehind keeps it from matching the
# tail of a longer token, and a path never starts "//", which is the rest of a
# web address ("https://host/a.pdf" names no file on disk); the trailing \b
# keeps "report.docx." clean. A file:// address names the path after its
# scheme.
_ABS_PATH_RE = re.compile(rf"(?<![\w/.])((?:~|/(?!/))[^\s`'\"()\[\]<>]*?{_EXT})\b", re.IGNORECASE)
_FILE_URL_RE = re.compile(rf"\bfile://((?:~|/)[^\s`'\"()\[\]<>]*?{_EXT})\b", re.IGNORECASE)
# Where the reply marks both ends of a path, the path may hold spaces, as Mac
# file names often do ("~/Downloads/Q3 Report.docx"): inside backticks or
# double quotes, as a markdown link's target (bare, <angle-bracketed>, or
# url-encoded, which is decoded), or as a whole table cell. A marked run that
# holds a second path or a path and more words ("~/a.md and ~/b.md") is not
# one path, and the bare paths inside it are read instead.
_BACKTICK_PATH_RE = re.compile(rf"`((?:~|/)[^`\n]*?{_EXT})`", re.IGNORECASE)
_QUOTED_PATH_RE = re.compile(
    rf"[\"\u201c]((?:~|/)[^\"\u201c\u201d\n]*?{_EXT})[\"\u201d]", re.IGNORECASE
)
_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(\s*(?:<([^<>\n]+)>|([^()<>\n]+?))\s*\)")
_CELL_PATH_RE = re.compile(rf"\s*[*_]*((?:~|/)[^|`\"<>\n]*?{_EXT})[*_]*\s*", re.IGNORECASE)
_WHOLE_PATH_RE = re.compile(rf"(?:~|/).*{_EXT}", re.IGNORECASE)
_TWO_PATHS_RE = re.compile(rf"\s(?:~|/)|{_EXT}\s", re.IGNORECASE)
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
# with the line that introduces it ("I created these files:"). The words of a
# file name are not the reply's words ("~/Not Final.docx" negates nothing),
# and a comma or dash inside a marked path does not split its clause.
#
# A markdown table whose header names a file, path, location, output or saved
# column lists deliverables: each of its rows is read whole, with the table's
# lead-in line, and every path in a row is claimed unless the row negates,
# offers or asks. The rows of any other table read as prose, as before.
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_ABBREVIATIONS = {"e.g.": "for example", "i.e.": "that is"}
_ABBREV_RE = re.compile(r"\b(?:e\.g|i\.e)\.", re.IGNORECASE)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_TABLE_RULE_RE = re.compile(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*")
_FILE_COLUMN_RE = re.compile(
    r"\b(?:file(?:name|path)?s?|paths?|locations?|outputs?|saved)\b", re.IGNORECASE
)
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
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>()\[\]`\"']+", re.IGNORECASE)
_MASK = "\0"

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


# ── Finding the paths a text names ─────────────────────────────────────────


class _Span(NamedTuple):
    """One path a text names: where its mention starts and ends (a link's
    "[", an opening quote, or the path itself), the path, whether it is the
    app's own file link, and where the part that is the path's own text
    begins (a link's prose label, "here", stays readable)."""

    start: int
    end: int
    path: str
    app_link: bool
    mask_from: int


def _free(spans: list[_Span], start: int, end: int) -> bool:
    return all(end <= s.start or start >= s.end for s in spans)


def _link_path(target: str) -> tuple[str, bool] | None:
    """The file a markdown link's target names, decoded, and whether it is the
    app's own file link; None for any other target (a web page, an anchor, a
    relative name). The app's link splits at its first raw "&" before
    decoding, as the chat's handler does."""
    target = target.strip()
    if target.startswith(_WSFILE):
        path = unquote(target[len(_WSFILE) :].split("&", 1)[0]).strip()
        return (path, True) if path else None
    if target[:7].lower() == "file://":
        target = target[7:]
    path = unquote(target)
    if _WHOLE_PATH_RE.fullmatch(path) and not _TWO_PATHS_RE.search(path):
        return path, False
    return None


def _path_spans(text: str) -> list[_Span]:
    """Every path ``text`` names as a deliverable, in text order: a marked
    path first (a link target, backticks, double quotes), then a file://
    address, the app's link and a bare path outside those."""
    spans: list[_Span] = []
    for m in _LINK_RE.finditer(text):
        found = _link_path(m.group(2) or m.group(3) or "")
        if found and _free(spans, *m.span()):
            # A label that names a file is part of the name; "here" is prose.
            label_is_name = bool(_EXT_RE.search(m.group(1)))
            mask_from = m.start() if label_is_name else m.end(1)
            spans.append(_Span(m.start(), m.end(), found[0], found[1], mask_from))
    for pattern in (_BACKTICK_PATH_RE, _QUOTED_PATH_RE):
        for m in pattern.finditer(text):
            if not _TWO_PATHS_RE.search(m.group(1)) and _free(spans, *m.span()):
                spans.append(_Span(m.start(), m.end(), m.group(1), False, m.start()))
    for m in _FILE_URL_RE.finditer(text):
        if _free(spans, *m.span()):
            spans.append(_Span(m.start(), m.end(), unquote(m.group(1)), False, m.start()))
    for m in _WSFILE_RE.finditer(text):
        if _free(spans, *m.span()):
            spans.append(_Span(m.start(), m.end(), unquote(m.group(1)), True, m.start()))
    for m in _ABS_PATH_RE.finditer(text):
        if _free(spans, *m.span(1)):
            spans.append(_Span(m.start(1), m.end(1), m.group(1), False, m.start(1)))
    return sorted(s._replace(path=s.path.strip()) for s in spans if s.path.strip())


def _row_spans(row: str) -> list[_Span]:
    """The paths a table row names: those ``_path_spans`` finds, and a cell
    that holds nothing but a path, spaces and all, since the pipes mark its
    ends."""
    spans = _path_spans(row)
    at = 0
    for cell in row.split("|"):
        m = _CELL_PATH_RE.fullmatch(cell)
        if m and not _TWO_PATHS_RE.search(m.group(1)):
            start, end = at + m.start(1), at + m.end(1)
            if _free(spans, start, end):
                spans.append(_Span(start, end, m.group(1), False, start))
        at += len(cell) + 1
    return sorted(spans)


def _masked(text: str, spans: list[_Span]) -> str:
    """``text`` with each path's own text, and each web address, hidden
    behind a run of NULs of the same length, so the words of a file name or
    an address ("example.com") read as neither a negation nor a claim and
    every position stays put."""
    chars = list(text)
    hidden = [(s.mask_from, s.end) for s in spans] + [m.span() for m in _URL_RE.finditer(text)]
    for start, end in hidden:
        chars[start:end] = _MASK * (end - start)
    return "".join(chars)


def claimed_paths(text: str) -> list[str]:
    """File paths the text names as deliverables, in order, deduplicated."""
    out: list[str] = []
    for s in _path_spans(text or ""):
        if s.path not in out:
            out.append(s.path)
    return out


def _file_table_rows(lines: list[str]) -> dict[int, bool]:
    """Line index to True for each body row of a markdown table whose header
    names a file, path, location, output or saved column, and to False for
    that table's header and rule lines. The lines of any other table are left
    out, so they read as prose."""
    rows: dict[int, bool] = {}
    i = 0
    while i + 1 < len(lines):
        head, rule = lines[i], lines[i + 1]
        if "|" not in head or "|" not in rule or not _TABLE_RULE_RE.fullmatch(rule):
            i += 1
            continue
        end = i + 2
        while end < len(lines) and "|" in lines[end]:
            end += 1
        if _FILE_COLUMN_RE.search(head):
            rows.update(dict.fromkeys((i, i + 1), False))
            rows.update(dict.fromkeys(range(i + 2, end), True))
        i = end
    return rows


def _split_clauses(line: str) -> list[str]:
    """The clauses of one line. A split that falls inside a path's mention (a
    comma or " - " in a quoted file name) is part of the name."""
    spans = _path_spans(line)
    parts: list[str] = []
    start = 0
    for m in _CLAUSE_SPLIT_RE.finditer(line):
        if _free(spans, *m.span()):
            parts.append(line[start : m.start()])
            start = m.end()
    parts.append(line[start:])
    return parts


def _clauses(text: str):
    """(lead, clause, file_row) for each clause of each line outside code
    blocks. The lead is the line that introduces a list or a table, for its
    items; a row of a table that lists files is one clause of its own
    (``file_row``)."""
    text = _CODE_FENCE_RE.sub("\n", text or "")
    text = _ABBREV_RE.sub(lambda m: _ABBREVIATIONS[m.group(0).lower()], text)
    lines = text.splitlines()
    table = _file_table_rows(lines)
    lead = ""
    for i, line in enumerate(lines):
        if not line.strip() or table.get(i) is False:
            continue
        if table.get(i):
            yield lead, line, True
            continue
        item = bool(_LIST_ITEM_RE.match(line))
        if not item:
            lead = line if line.rstrip().endswith(":") else ""
        for clause in _split_clauses(line):
            if clause and clause.strip():
                yield (lead if item else ""), clause, False


def _is_question(clause: str) -> bool:
    return clause.rstrip(" \t\"')]*_`").endswith("?")


def _row_claims(lead: str, row: str) -> list[str]:
    """The paths a row of a file table claims: all of them, unless the row
    (with the table's lead-in line) negates, offers or asks."""
    spans = _row_spans(row)
    said = f"{lead} {_masked(row, spans)}"
    if (
        any(_is_question(cell) for cell in said.split("|"))
        or _NOT_MADE_RE.search(said)
        or _OFFER_AHEAD_RE.search(said)
    ):
        return []
    return [s.path for s in spans]


def asserted_paths(text: str) -> list[str]:
    """The paths ``text`` says were made (see the notes above
    _CODE_FENCE_RE); claimed_paths less those it only offers, plans, supposes,
    negates or asks about."""
    out: list[str] = []
    prev_claimed = False
    for lead, clause, file_row in _clauses(text):
        if file_row:
            claimed = _row_claims(lead, clause)
            out += [p for p in dict.fromkeys(claimed) if p not in out]
            prev_claimed = bool(claimed)
            continue
        spans = _path_spans(clause)
        if not spans:
            prev_claimed = False
            continue
        masked = _masked(clause, spans)
        said = f"{lead} {masked}"
        # "Saved to X, Y and Z": a clause of paths alone continues the last.
        paths_only = not _PATHS_ONLY_RE.sub("", masked).strip()
        claimed_here = False
        if not _is_question(clause) and not _NOT_MADE_RE.search(said):
            done = bool(_DONE_RE.search(said))
            for s in spans:
                ahead = masked[: s.start]
                if _OFFER_AHEAD_RE.search(f"{lead} {ahead}"):
                    continue
                if (
                    s.app_link
                    or done
                    or _LOCATED_RE.search(_LINK_OPEN_RE.sub("", ahead))
                    or (paths_only and prev_claimed)
                ):
                    claimed_here = True
                    if s.path not in out:
                        out.append(s.path)
        prev_claimed = claimed_here
    return out


def claims_an_artifact(text: str) -> bool:
    """True when ``text`` points at an artifact card as made, as opposed to
    offering one, asking about one or saying there is none."""
    for lead, clause, _file_row in _clauses(text):
        said = _masked(clause, _path_spans(clause))
        m = _ARTIFACT_CLAIM_RE.search(said)
        if (
            m
            and not _is_question(clause)
            and not _NOT_MADE_RE.search(f"{lead} {said}")
            and not _OFFER_AHEAD_RE.search(f"{lead} {said[: m.start()]}")
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
