"""Artifacts the assistant built in a session: read them back, edit in place.

``create_artifact`` hands the user a single-file HTML app as a card. Until
now that HTML left the model's context the moment the turn ended (the
persisted history keeps assistant text, not tool-call inputs), so "add
English to it" meant regenerating 80 KB from scratch: minutes of silence and
a dollar of output tokens per change, with nothing to show meanwhile. In one
real session the model even searched its own past sessions for the HTML.

Two tools close that gap:

``read_artifact``   the current HTML of an artifact from this session (whole
                    when it fits, else an outline plus a line range)
``edit_artifact``   targeted old_text/new_text replacements applied to that
                    HTML; the updated card replaces the previous one

Versions live in memory per session (latest last) and fall back to the
``programArtifact`` fields the frontend persists on assistant messages, so
an artifact from an earlier app launch is still editable.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone

log = logging.getLogger("whisper-studio")

MAX_VERSIONS_PER_SESSION = 20
_MAX_SESSIONS = 256
READ_FULL_MAX_CHARS = 40_000  # whole HTML returned when at or under this
READ_RANGE_MAX_LINES = 400
READ_RANGE_MAX_CHARS = 40_000
OUTLINE_MAX_ENTRIES = 80
MAX_EDITS = 40

ARTIFACT_TOOL_FAMILY: frozenset[str] = frozenset(
    {"create_artifact", "read_artifact", "edit_artifact"}
)

_lock = threading.Lock()
_versions: dict[str, list[dict]] = {}

_OUTLINE_RE = re.compile(
    r"^\s*(?:<(?:section|header|main|footer|nav|aside|h1|h2|h3|script|style|template|form|table)\b"
    r"|<\w+[^>]*\bid=\"[^\"]+\"|function\s+\w+|(?:const|let|class)\s+\w+\s*=?\s*(?:\(|\{|function|class|new)?)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_artifact(
    session_id: str, *, title: str, html: str, description: str = "", tool_use_id: str = ""
) -> dict:
    """Remember a created or edited artifact as the session's latest version."""
    entry = {
        "title": str(title or "Untitled Artifact"),
        "description": str(description or ""),
        "html": html if isinstance(html, str) else str(html or ""),
        "tool_use_id": tool_use_id or "",
        "timestamp": _now(),
        "source": "turn",
    }
    if not session_id:
        return entry
    with _lock:
        if len(_versions) >= _MAX_SESSIONS and session_id not in _versions:
            _versions.pop(next(iter(_versions)), None)
        versions = _versions.pop(session_id, [])
        versions.append(entry)
        del versions[:-MAX_VERSIONS_PER_SESSION]
        _versions[session_id] = versions
    return entry


def forget_session(session_id: str) -> None:
    with _lock:
        _versions.pop(session_id, None)


def _history_artifacts(session_id: str) -> list[dict]:
    """Artifacts the frontend persisted on assistant messages, oldest first."""
    if not session_id:
        return []
    try:
        from server.infrastructure.sessions import _get_conn

        with _get_conn() as conn:
            row = conn.execute(
                "SELECT chat_history FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        history = json.loads(row["chat_history"]) if row and row["chat_history"] else []
    except Exception as e:  # noqa: BLE001 - history is a fallback, never a failure
        log.debug("artifact history unavailable for %s: %s", session_id, e)
        return []
    out: list[dict] = []
    for msg in history or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        pa = msg.get("programArtifact")
        if isinstance(pa, dict) and isinstance(pa.get("html"), str) and pa["html"].strip():
            out.append(
                {
                    "title": str(pa.get("title") or "Untitled Artifact"),
                    "description": str(pa.get("description") or ""),
                    "html": pa["html"],
                    "tool_use_id": str(pa.get("tool_use_id") or ""),
                    "timestamp": str(msg.get("timestamp") or ""),
                    "source": "history",
                }
            )
    return out


def list_artifacts(session_id: str) -> list[dict]:
    """Every known version, latest first: this process's memory first, then
    the persisted history (the same title with identical HTML counts once)."""
    with _lock:
        mem = list(reversed(_versions.get(session_id, [])))
    hist = list(reversed(_history_artifacts(session_id)))
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for a in mem + hist:
        key = (a["title"], a["html"])
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def latest_artifact(session_id: str, title: str | None = None) -> dict | None:
    """The newest artifact, or the newest whose title matches ``title``
    (case-insensitive, substring)."""
    versions = list_artifacts(session_id)
    if not versions:
        return None
    if title and title.strip():
        needle = title.strip().lower()
        for a in versions:
            if needle == a["title"].lower():
                return a
        for a in versions:
            if needle in a["title"].lower():
                return a
        return None
    return versions[0]


def _outline(lines: list[str]) -> list[str]:
    out: list[str] = []
    for i, line in enumerate(lines, start=1):
        if _OUTLINE_RE.match(line):
            text = line.strip()
            out.append(f"L{i}: {text[:90]}" + (" ..." if len(text) > 90 else ""))
            if len(out) >= OUTLINE_MAX_ENTRIES:
                out.append("... (outline truncated)")
                break
    return out


def _numbered(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))


def execute_read_artifact(tool_input: dict, session_id: str) -> str:
    title = str(tool_input.get("title") or "").strip() or None
    art = latest_artifact(session_id, title)
    if art is None:
        known = [a["title"] for a in list_artifacts(session_id)]
        if title and known:
            return json.dumps({"error": f"no artifact titled {title!r}", "artifacts": known})
        return json.dumps(
            {"error": "no artifact exists in this session yet; create one with create_artifact"}
        )
    html = art["html"]
    lines = html.split("\n")
    total_lines = len(lines)
    base = {
        "title": art["title"],
        "description": art["description"],
        "total_chars": len(html),
        "total_lines": total_lines,
        "source": art["source"],
        "hint": (
            "Change it with edit_artifact (old_text must be unique) instead of regenerating; "
            "the card updates in place."
        ),
    }
    start = tool_input.get("start_line")
    end = tool_input.get("end_line")
    if start is None and end is None:
        if len(html) <= READ_FULL_MAX_CHARS:
            return json.dumps({**base, "html": html})
        head_end = min(total_lines, 120)
        return json.dumps(
            {
                **base,
                "note": (
                    f"The HTML is {len(html)} chars; too large to return whole. Below are the "
                    f"first {head_end} lines and an outline of landmarks. Read any region with "
                    f"start_line/end_line (at most {READ_RANGE_MAX_LINES} lines per call)."
                ),
                "outline": _outline(lines),
                "lines": _numbered(lines, 1, head_end),
            }
        )
    try:
        s = max(1, int(start or 1))
        e = int(end) if end is not None else s + READ_RANGE_MAX_LINES - 1
    except (TypeError, ValueError):
        return json.dumps({"error": "start_line and end_line must be integers"})
    e = min(total_lines, max(s, e), s + READ_RANGE_MAX_LINES - 1)
    if s > total_lines:
        return json.dumps(
            {**base, "error": f"start_line {s} is past the end ({total_lines} lines)"}
        )
    chunk = _numbered(lines, s, e)
    truncated = False
    if len(chunk) > READ_RANGE_MAX_CHARS:
        chunk = chunk[:READ_RANGE_MAX_CHARS] + "\n... (range truncated at the char cap)"
        truncated = True
    return json.dumps(
        {**base, "start_line": s, "end_line": e, "truncated": truncated, "lines": chunk}
    )


def apply_edits(html: str, edits: list[dict]) -> tuple[str | None, str | None, int]:
    """Apply ``edits`` sequentially. Returns (new_html, error, applied).
    Atomic: any failing edit leaves the original untouched."""
    work = html
    applied = 0
    for i, edit in enumerate(edits, start=1):
        if not isinstance(edit, dict):
            return None, f"edit {i} is not an object", applied
        old = edit.get("old_text")
        new = edit.get("new_text", "")
        if not isinstance(old, str) or not old:
            return None, f"edit {i}: old_text is required and must be non-empty", applied
        if not isinstance(new, str):
            new = str(new)
        count = work.count(old)
        if count == 0:
            return None, f"edit {i}: old_text was not found: {old[:120]!r}", applied
        if count > 1 and not edit.get("replace_all"):
            return (
                None,
                f"edit {i}: old_text matches {count} places; include more surrounding text "
                "or set replace_all",
                applied,
            )
        work = work.replace(old, new) if edit.get("replace_all") else work.replace(old, new, 1)
        applied += 1
    return work, None, applied


def execute_edit_artifact(
    tool_input: dict, session_id: str, tool_use_id: str = ""
) -> tuple[str, list[dict]]:
    """Returns (tool output, side effects). A successful edit emits the same
    ``program_artifact`` side effect ``create_artifact`` does, so the updated
    card replaces the previous one in the chat."""
    title = str(tool_input.get("title") or "").strip() or None
    edits = tool_input.get("edits")
    if not isinstance(edits, list) or not edits:
        return json.dumps({"error": "edits must be a non-empty list of {old_text, new_text}"}), []
    if len(edits) > MAX_EDITS:
        return json.dumps({"error": f"at most {MAX_EDITS} edits per call"}), []
    art = latest_artifact(session_id, title)
    if art is None:
        return (
            json.dumps(
                {"error": "no artifact to edit in this session; create one with create_artifact"}
            ),
            [],
        )
    new_html, error, applied = apply_edits(art["html"], edits)
    if error:
        return (
            json.dumps(
                {
                    "error": error,
                    "applied": 0,
                    "hint": "Nothing was changed. Call read_artifact to copy the exact text.",
                }
            ),
            [],
        )
    new_title = str(tool_input.get("new_title") or "").strip() or art["title"]
    description = str(tool_input.get("description") or "").strip() or art["description"]
    record_artifact(
        session_id, title=new_title, html=new_html, description=description, tool_use_id=tool_use_id
    )
    side_effects = [
        {
            "program_artifact": {
                "title": new_title,
                "html": new_html,
                "description": description,
                "tool_use_id": tool_use_id,
            }
        }
    ]
    output = (
        f"Artifact '{new_title}' updated: {applied} edit(s) applied, HTML now {len(new_html)} chars "
        f"({new_html.count(chr(10)) + 1} lines). The updated card replaces the previous one in the "
        "chat; describe the change briefly and do not paste the HTML."
    )
    return output, side_effects


def activate_artifact_tools(session_id: str) -> None:
    """Once an artifact exists, its read and edit tools are advertised."""
    try:
        from server.chat.tool_activation import activate

        activate(session_id, ["read_artifact", "edit_artifact"])
    except Exception as e:  # noqa: BLE001
        log.debug("artifact tool activation skipped: %s", e)


READ_ARTIFACT_TOOL = {
    "name": "read_artifact",
    "description": (
        "Read the current HTML of an artifact you created earlier in this session (the "
        "latest one, or by title). Use it before changing an existing app instead of "
        "rebuilding it from memory. Small artifacts come back whole; large ones return an "
        "outline plus the first lines, and start_line/end_line read any region."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Artifact title (default: the latest)"},
            "start_line": {"type": "integer", "description": "First line to return (1-based)"},
            "end_line": {"type": "integer", "description": "Last line to return (inclusive)"},
        },
        "required": [],
    },
}

EDIT_ARTIFACT_TOOL = {
    "name": "edit_artifact",
    "description": (
        "Change an artifact you created earlier in this session IN PLACE with targeted text "
        "replacements, instead of regenerating the whole HTML. Each edit replaces one unique "
        "old_text with new_text (set replace_all for repeated text). All edits apply "
        "atomically; the updated card replaces the previous one. Call read_artifact first to "
        "copy exact text. Use create_artifact only for a full rewrite."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Artifact title (default: the latest)"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string", "description": "Exact existing text"},
                        "new_text": {"type": "string", "description": "Replacement (may be empty)"},
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace every occurrence (default: must be unique)",
                        },
                    },
                    "required": ["old_text", "new_text"],
                },
                "description": "Replacements, applied in order",
            },
            "new_title": {"type": "string", "description": "Optional new card title"},
            "description": {"type": "string", "description": "Optional new card description"},
        },
        "required": ["edits"],
    },
}

ARTIFACT_TOOLS = [READ_ARTIFACT_TOOL, EDIT_ARTIFACT_TOOL]

__all__ = [
    "ARTIFACT_TOOLS",
    "ARTIFACT_TOOL_FAMILY",
    "EDIT_ARTIFACT_TOOL",
    "READ_ARTIFACT_TOOL",
    "activate_artifact_tools",
    "apply_edits",
    "execute_edit_artifact",
    "execute_read_artifact",
    "forget_session",
    "latest_artifact",
    "list_artifacts",
    "record_artifact",
]
