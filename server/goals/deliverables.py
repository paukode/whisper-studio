"""Claimed-deliverable check for the completion gate.

The model's final reply routinely says "Saved to `/path/report.docx`" or "the
artifact card above" on its own belief. In two real sessions those were false:
a workflow's file was never written, and no artifact had been created, and the
user found out only by asking. This module reads the turn's replies (the
reply being gated, one a pending mid-turn message kept from the gate, the
first half of a max_tokens split), pulls out every file path they say was
made, and checks the file exists and is non-empty (workspace-relative paths
resolve against the workspace). An artifact-card claim is met by a
``create_artifact`` or ``edit_artifact`` call in the same turn, or by a card
the session already holds from an earlier turn, which the caller looks up
and passes in. A miss becomes gate feedback, and the turn continues until
the file exists or the reply is corrected, at most MAX_CLAIM_NUDGES times per
turn.

Pure functions over the provider-neutral message list; no I/O beyond stat.
"""

from __future__ import annotations

import json
import os

from server.goals.claims import claimed_paths, claims_an_artifact
from server.goals.tail import _render_blocks

# Claims are read out of prose, so a wrong reading must cost a round, never
# a loop to the cap: a second nudge only after the model acted on the first
# (check_claims).
MAX_CLAIM_NUDGES = 2
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
# The salvage round's note (runner.py) and a scheduled run's verifier row
# (cron_run.py) carry on the turn too.
_SALVAGE_PREFIX = "[The conversation no longer fits"
_VERIFY_PREFIX = "[verify]"
_ENGINE_PREFIXES = (
    _GATE_PREFIX,
    _MIDTURN_OPEN,
    _REMINDER_OPEN,
    _CONTINUE_PREFIX,
    _SALVAGE_PREFIX,
    _VERIFY_PREFIX,
)
_TOOL_RESULT_TYPES = ("tool_result", "function_call_output")
_ARTIFACT_TOOLS = ("create_artifact", "edit_artifact")

# Tools that can write a file outside the workspace, and where a call names
# the file it writes. Plan mode refuses only the ws_* workspace writers
# (tool_executor._PLAN_MODE_BLOCKED) and these still run there behind their
# approval card, so a path the reply says it saved is checked in plan mode
# when one of these calls named it (``targeted_paths``); any other path there
# is a file the plan will write. A document tool names its file in its
# arguments and adds its extension to a bare one; a script or a command names
# it somewhere in its text. The scripts can also write a file again under a
# name the turn used before (requested_files).
_DOCUMENT_TOOLS = {
    "create_docx": ".docx",
    "create_pptx": ".pptx",
    "create_xlsx": ".xlsx",
    "create_pdf": ".pdf",
    "office_script": "",
}
_COMMAND_TEXT = {
    "run_python": "code",
    "terminal_run": "command",
    "terminal_send": "input",
    "aws_cli": "command",
}
OUTSIDE_WRITERS = frozenset({"save_file", *_DOCUMENT_TOOLS, *_COMMAND_TEXT})


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


def asked_by_row(messages: list) -> list[tuple[int, str]]:
    """What the user asked for this turn, piece by piece, each with the row
    of the turn (``turn_messages`` order) that carries it: the real prompt it
    is answering, then the words of each message the user sent while it ran.
    Words folded onto the prompt (loop_hints.inject_reminder) come with the
    prompt's row, -1, just before the turn's first; words folded onto a later
    row (a tool result, or a row of their own after an assistant tail) come
    with that row. Gate feedback, tool results and engine reminders, agent
    reports among them, are never part of it."""
    for i in range(len(messages) - 1, -1, -1):
        if _is_user_prompt(messages[i]):
            break
    else:
        return []
    asked = [
        (-1, _midturn_words(t) or t)
        for t in _texts(messages[i].get("content"))
        if not t.startswith(_REMINDER_OPEN)
    ]
    asked += [
        (row, words)
        for row, m in enumerate(messages[i + 1 :])
        if isinstance(m, dict) and m.get("role") == "user"
        for t in _texts(m.get("content"))
        if (words := _midturn_words(t))
    ]
    return [(row, t) for row, t in asked if t]


def last_user_prompt(messages: list) -> str:
    """What the user asked for this turn (``asked_by_row``) as one text."""
    return "\n\n".join(t for _row, t in asked_by_row(messages))


def called_tools(rows: list) -> list[str]:
    """The names of the tools the assistant rows among ``rows`` call (an
    Anthropic tool_use block or a Responses function_call item), in order."""
    return [
        str(b.get("name") or "")
        for m in rows
        if isinstance(m, dict)
        and m.get("role") == "assistant"
        and isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") in ("tool_use", "function_call")
    ]


def _calls_a_tool(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") in ("tool_use", "function_call") for b in content
    )


def reply_texts(messages: list, *, since: int = 0) -> list[str]:
    """The text of each reply of this turn, oldest first: the assistant rows
    that call no tool, and the last assistant row (the reply being gated)
    whatever it holds. A reply a pending mid-turn message kept from the gate
    is one of them, and the two halves of a max_tokens split are joined into
    one, as the chat shows them. With ``since``, only the replies from that
    row of the turn (``turn_messages`` order) on."""
    rows: list[str] = []
    joins_next = False
    turn = [m for m in turn_messages(messages)[since:] if isinstance(m, dict)]
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


def verified_deliveries(history) -> list[dict]:
    """The deliveries the chat's earlier replies verified: the chips each
    assistant row of the history carries (the chat sends them with it, and
    they never reach the model's prompt), oldest first."""
    out: list[dict] = []
    for m in history if isinstance(history, list) else []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for d in m.get("deliveries") or []:
            if isinstance(d, dict) and d.get("kind"):
                out.append({k: str(d.get(k) or "") for k in ("kind", "target", "detail", "href")})
    return out[-500:]


def resolve_path(path: str, workspace: str | None) -> str:
    """Where ``path`` points: the home folder expanded, a workspace-relative
    path joined to the workspace, and the result normalised, so two spellings
    of one file compare equal."""
    resolved = os.path.expanduser(path)
    if not os.path.isabs(resolved) and workspace:
        resolved = os.path.join(workspace, resolved)
    return os.path.normpath(resolved)


def exists_non_empty(path: str, workspace: str | None) -> bool:
    """True when the path resolves to a file with content (workspace-relative
    paths resolve against the workspace)."""
    resolved = resolve_path(path, workspace)
    try:
        return os.path.isfile(resolved) and os.path.getsize(resolved) > 0
    except OSError:
        return False


def _assistant_strings(content) -> list[str]:
    """What one assistant row says: its text blocks, and each tool call's
    arguments."""
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") in ("tool_use", "function_call"):
            out.append(json.dumps(call_input(b), ensure_ascii=False, default=str))
        else:
            out.append(str(b.get("text", "")))
    return out


def assistant_named_paths(rows: list, workspace: str | None) -> set[str]:
    """Every deliverable path the assistant rows among ``rows`` name, in a
    reply or in a tool call's arguments, resolved (``resolve_path``). A user
    row names none: the user's own words, even when they give the path of the
    file they want, and a tool's result are not the assistant naming a file
    it made."""
    return {
        resolve_path(p, workspace)
        for m in rows
        if isinstance(m, dict) and m.get("role") == "assistant"
        for text in _assistant_strings(m.get("content"))
        for p in claimed_paths(text)
    }


def artifact_created(messages: list) -> bool:
    """True if this turn contains a create_artifact or edit_artifact call
    (Anthropic tool_use block or Responses function_call item); both put a
    card in the chat."""
    return any(name in _ARTIFACT_TOOLS for name in called_tools(turn_messages(messages)))


def artifact_claim_unmet(messages: list) -> bool:
    """True when a reply of this turn points at an artifact card and no call
    this turn made one. Whether the session already holds a card from an
    earlier turn is the caller's to look up (check_claims'
    ``session_has_artifact``): that lookup reads storage, and it is needed
    only when this is True."""
    return claims_an_artifact("\n".join(reply_texts(messages))) and not artifact_created(messages)


def call_input(block: dict) -> dict:
    """A tool call's arguments: an Anthropic block's input, or a Responses
    item's arguments, which arrive as JSON text."""
    given = block.get("input", block.get("arguments"))
    if isinstance(given, str):
        try:
            given = json.loads(given)
        except ValueError:
            return {}
    return given if isinstance(given, dict) else {}


def _write_targets(name: str, given: dict) -> list[str]:
    """The files one call of a tool in OUTSIDE_WRITERS writes, as its executor
    resolves them: a save's destination (a folder gets the file's own name) or
    else the folder it suggests; a document tool's destination, or else its
    workspace path or the Documents folder, with the extension the tool adds
    to a bare name; each deliverable path in a script's code or a command's
    text."""
    if name in _COMMAND_TEXT:
        return claimed_paths(str(given.get(_COMMAND_TEXT[name]) or ""))
    dest = str(given.get("destination_path") or "").strip()
    if name == "save_file":
        filename = os.path.basename(str(given.get("filename") or "").strip())
        if dest:
            return [dest, os.path.join(dest, filename)] if filename else [dest]
        folder = given.get("suggested_location")
        folder = folder if folder in ("Documents", "Downloads") else "Documents"
        return [f"~/{folder}/{filename}"] if filename else []
    if name not in _DOCUMENT_TOOLS:
        return []
    path = str(given.get("path") or "").strip()
    named = [dest] if dest else [path, f"~/Documents/{os.path.basename(path) or 'document'}"]
    ext = _DOCUMENT_TOOLS[name]
    return [
        target
        for p in named
        if p
        for target in ([p] if not ext or p.lower().endswith(ext) else [p, p + ext])
    ]


def targeted_paths(messages: list, workspace: str | None) -> set[str]:
    """The files this turn's calls of the tools in OUTSIDE_WRITERS named as
    what they write, resolved (``resolve_path``)."""
    return {
        resolve_path(p, workspace)
        for m in turn_messages(messages)
        if isinstance(m, dict)
        and m.get("role") == "assistant"
        and isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict)
        and b.get("type") in ("tool_use", "function_call")
        and b.get("name") in OUTSIDE_WRITERS
        for p in _write_targets(str(b["name"]), call_input(b))
        if p
    }


def _is_claim_nudge(m) -> bool:
    return (
        isinstance(m, dict)
        and m.get("role") == "user"
        and any(t.startswith(f"{_GATE_PREFIX} {CLAIM_MARKER}") for t in _texts(m.get("content")))
    )


def claim_nudges_used(messages: list) -> int:
    """How many claim nudges the gate already issued this turn."""
    return sum(1 for m in turn_messages(messages) if _is_claim_nudge(m))


def _acted_since_last_nudge(messages: list) -> bool:
    """True when an assistant row after the turn's last claim nudge called a
    tool: the model tried to make its claim true rather than only answer."""
    turn = turn_messages(messages)
    last = max((i for i, m in enumerate(turn) if _is_claim_nudge(m)), default=-1)
    return last >= 0 and any(
        isinstance(m, dict) and m.get("role") == "assistant" and _calls_a_tool(m.get("content"))
        for m in turn[last + 1 :]
    )


def _settled_later(claim, later: list[str]) -> bool:
    """True when a later reply of the turn corrects the claim: a sentence
    that names its target and says it was not so (the claim made again is
    checked where it is made, and an instruction that names it, "Open
    `q3.docx`", corrects nothing). The stream guard notes the same way."""
    from server.goals.evidence import mentions_as_correction

    return any(mentions_as_correction(claim.target, text) for text in later)


def _shown(clause: str) -> str:
    clause = " ".join(clause.split())
    return clause if len(clause) <= 160 else clause[:157] + "..."


def check_claims(
    messages: list,
    workspace: str | None,
    *,
    plan_mode: bool = False,
    session_has_artifact: bool = False,
    max_attempts: int = MAX_CLAIM_NUDGES,
    started_at: float | None = None,
    calls: list | None = None,
    receipts: list | None = None,
) -> str | None:
    """Gate feedback naming what this turn's replies claim was delivered but
    was not, or None when every claim holds: server/goals/claims.py reads the
    claims and server/goals/evidence.py checks each one (a file written since
    ``started_at`` where the reply says it was made, an act done by a call
    of the turn that succeeded). Every reply of the turn is read, so one the
    gate never judged (a pending mid-turn message, or a Stop hook that
    blocked first) is checked too; a reply that passed passes again.
    ``calls`` are the turn's calls the stream guard kept, which compaction
    may have taken out of ``messages``; ``receipts`` the deliveries earlier
    replies verified, which a recap may rest on.

    The stream guard (server/chat/claim_guard.py) held each sentence making
    such a claim back from the user, so the feedback says the user has not
    seen it. A second nudge follows only when the model acted on the first
    (it called a tool since): a reply that only corrected itself is not
    asked again, and the guard notes to the user what did not happen.

    An artifact-card claim is met by a card this turn made or, with
    ``session_has_artifact``, by one the session holds from an earlier turn:
    a reply about that card claims nothing false, and holding it only made
    the model rebuild the card.

    Plan mode refuses the workspace writes, so a path there is usually a
    file the plan will write: only a path that a call this turn of a tool
    that writes outside the workspace named as its target is checked
    (``targeted_paths``), and an artifact-card claim always is. A look around
    with terminal_run makes no planned file a claim."""
    used = claim_nudges_used(messages)
    if used >= max_attempts or (used and not _acted_since_last_nudge(messages)):
        return None
    replies = reply_texts(messages)
    if not replies:
        return None
    from server.goals import claims as c
    from server.goals.evidence import Evidence, check

    ev = Evidence.of(
        messages,
        workspace=workspace,
        started_at=started_at,
        session_has_artifact=session_has_artifact,
        plan_mode=plan_mode,
        extra_calls=calls,
        receipts=receipts,
    )
    failed: dict[tuple[str, str], str] = {}
    running = False
    for i, reply in enumerate(replies):
        for claim in c.read_claims(reply):
            if (claim.kind, claim.target) in failed or _settled_later(claim, replies[i + 1 :]):
                continue
            verdict = check(claim, ev)
            if not verdict.ok:
                running = running or verdict.pending
                note = verdict.note
                if claim.kind == c.ARTIFACT:
                    note += " (no create_artifact call has made an artifact card)"
                failed[(claim.kind, claim.target)] = f'- "{_shown(claim.clause)}": {note}'
    if not failed:
        return None
    lines = list(failed.values())
    shown = lines[:6] + ([f"- and {len(lines) - 6} more"] if len(lines) > 6 else [])
    written = sorted(p for p in targeted_paths(messages, workspace) if exists_non_empty(p, None))
    tail = f"\nFiles this turn's calls wrote: {', '.join(written[:5])}." if written else ""
    if running:
        # A call that is still running may yet do it: starting it again
        # would do it twice.
        tail = (
            "\nA call of this turn that started one of these has not finished: check on it "
            "(task_status, task_output, terminal_send) and never start it again." + tail
        )
    return (
        f"{CLAIM_MARKER} These sentences of your reply were held back from the user, because "
        "this turn's tool results do not show that what they claim happened:\n"
        + "\n".join(shown)
        + "\nThe user has not seen them. Never repeat an action that may already have happened "
        "(a message, a push, an upload, a payment): if it was done in an earlier turn, say so "
        "plainly. If the user asked for it in this turn and it has not been done, do it now "
        "and confirm it from the tool result, then say it once. Otherwise tell the user "
        "plainly that it was not done. Do not mention the held sentences or apologize for "
        "them. Never report a delivery you have not verified." + tail
    )
