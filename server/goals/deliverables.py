"""Claimed-deliverable check for the completion gate.

The model's final reply routinely says "Saved to `/path/report.docx`" or "the
artifact card above" on its own belief. In two real sessions those were false:
a workflow's file was never written, and no artifact had been created, and the
user found out only by asking. This module reads the turn's final assistant
text, pulls out every file path it presents as a deliverable, and checks the
file exists and is non-empty (workspace-relative paths resolve against the
workspace). An artifact-card claim is checked against a ``create_artifact``
call in the same turn. A miss becomes gate feedback, and the turn continues
until the file exists or the reply is corrected (bounded by the gate cap).

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
_GATE_PREFIX = "[completion gate]"


def _is_user_prompt(m: dict) -> bool:
    """A real user turn, as opposed to tool results or gate feedback that the
    loop also files under role=user."""
    if m.get("role") != "user":
        return False
    content = m.get("content")
    if isinstance(content, str):
        return not content.startswith(_GATE_PREFIX)
    if isinstance(content, list):
        texts = [
            b for b in content if isinstance(b, dict) and b.get("type") in ("text", "input_text")
        ]
        others = [
            b
            for b in content
            if isinstance(b, dict) and b.get("type") not in ("text", "input_text")
        ]
        if others or not texts:
            return False
        return not str(texts[0].get("text", "")).startswith(_GATE_PREFIX)
    return False


def turn_messages(messages: list) -> list:
    """Messages since the last real user prompt (the current turn)."""
    for i in range(len(messages) - 1, -1, -1):
        if _is_user_prompt(messages[i]):
            return messages[i + 1 :]
    return list(messages)


def last_user_prompt(messages: list) -> str:
    """Text of the real user prompt this turn is answering, gate feedback and
    tool results excluded. Mid-turn messages are appended onto that same
    message (loop_hints.inject_reminder), so what the user asked for while the
    turn ran is part of it."""
    for i in range(len(messages) - 1, -1, -1):
        if _is_user_prompt(messages[i]):
            return _render_blocks(messages[i].get("content"))
    return ""


def last_assistant_text(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "assistant":
            return _render_blocks(m.get("content"))
    return ""


def claimed_paths(text: str) -> list[str]:
    """File paths the text presents as deliverables, in order, deduplicated."""
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


def check_claims(messages: list, workspace: str | None) -> str | None:
    """Gate feedback naming what the final reply claimed but did not produce,
    or None when every claim checks out."""
    text = last_assistant_text(messages)
    if not text:
        return None
    missing = [p for p in claimed_paths(text) if not exists_non_empty(p, workspace)]
    artifact_missing = bool(_ARTIFACT_CLAIM_RE.search(text)) and not artifact_created(messages)
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
        "; ".join(parts)
        + ". Produce it now and confirm from the tool result, or correct your reply to say it "
        "was not produced. Never report a deliverable you have not verified."
    )
