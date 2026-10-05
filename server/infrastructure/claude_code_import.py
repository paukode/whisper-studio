"""Read a Claude Code transcript (``~/.claude/projects/<project>/<id>.jsonl``)
into a Whisper chat history, so the session importer takes one as readily as
its own exports.

A transcript is a tree, not a list: every entry names its parent by uuid, a
rewind or an edited prompt starts a sibling branch, and /compact restarts the
chain at a boundary whose ``logicalParentUuid`` points back across it. The
conversation the user last saw is the path from the newest message back to
the root, so that path decides which prompts and replies are imported. Tool
results are matched by tool_use id across the whole file instead, because the
results of parallel tool calls hang off sibling branches of that path.

Only what a person reads is kept: typed prompts (including ones queued while
a turn ran), the assistant's text, thinking that was not redacted, and each
tool call with its result capped the way a live turn caps it, and the API
errors and usage limit notices the user was shown. Harness traffic (system
reminders, hook feedback, local command output, task notifications,
compaction summaries) is dropped.
"""

import json
import re

# Same cap tool_executor puts on the result preview a live turn persists.
TOOL_RESULT_PREVIEW_CHARS = 2000
FALLBACK_TITLE = "Claude Code session"
_TITLE_FROM_PROMPT_CHARS = 80

# Context the harness wraps around a typed prompt; the prompt is what is left.
_WRAPPER_RE = re.compile(r"<(system-reminder|ide_opened_file|ide_selection)>.*?</\1>", re.S)
# User rows that carry harness output, not something the user typed.
_HARNESS_PREFIXES = (
    "<local-command-",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<task-notification>",
)
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_INTERRUPTED = "[Request interrupted by user"
_STOPPED = "*(Stopped)*"


def _load(line: str) -> dict | None:
    """One JSONL line as a dict, or None. A live session's last line can be
    half written, so a bad line is skipped rather than fatal."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _is_message(entry: dict) -> bool:
    return entry.get("type") in ("user", "assistant") and isinstance(entry.get("message"), dict)


def is_transcript(raw: str) -> bool:
    """True when ``raw`` is a Claude Code transcript rather than a Whisper
    export. Decided by the first line that tells them apart, so a long file
    is not parsed twice."""
    for line in raw.splitlines():
        obj = _load(line)
        if obj is None:
            continue
        if obj.get("type") == "export_meta":
            return False
        if isinstance(obj.get("sessionId"), str) or (_is_message(obj) and obj.get("uuid")):
            return True
    return False


def _active_path(entries: list[dict], leaf: dict) -> set[str]:
    """Uuids on the path from ``leaf`` back to the root, crossing compaction
    boundaries. A repeated uuid ends the walk instead of looping on it."""
    by_uuid = {e["uuid"]: e for e in entries if e.get("uuid")}
    path: set[str] = set()
    node = leaf
    while node is not None and node["uuid"] not in path:
        path.add(node["uuid"])
        parent = node.get("parentUuid") or node.get("logicalParentUuid")
        node = by_uuid.get(parent) if parent else None
    return path


def _block_text(blocks: list) -> tuple[str, int]:
    """Joined text of a content block list, and how many images it held."""
    texts = [b.get("text") or "" for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    images = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "image")
    return "\n\n".join(t for t in texts if t), images


def _result_preview(content) -> str:
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif b.get("type") == "image":
                parts.append("[Image]")
        content = "\n".join(parts)
    text = content if isinstance(content, str) else ""
    if len(text) > TOOL_RESULT_PREVIEW_CHARS:
        return text[:TOOL_RESULT_PREVIEW_CHARS] + "..."
    return text


def _tool_results(messages: list[dict]) -> dict[str, tuple[str, bool]]:
    results: dict[str, tuple[str, bool]] = {}
    for entry in messages:
        content = entry["message"].get("content")
        if entry["type"] != "user" or not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                results[b["tool_use_id"]] = (
                    _result_preview(b.get("content")),
                    bool(b.get("is_error")),
                )
    return results


def _typed_text(content) -> tuple[str, bool] | None:
    """``(text, is_command)`` for what the user typed, with harness wrappers
    removed, or None when the content is harness traffic. A slash command
    reads as ``/name args``."""
    if isinstance(content, str):
        text, images = content, 0
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text, images = _block_text(content)
    else:
        return None
    text = _WRAPPER_RE.sub("", text).strip()
    if text.startswith(_HARNESS_PREFIXES):
        return None
    if text.startswith("<command-"):
        name = _COMMAND_NAME_RE.search(text)
        args = _COMMAND_ARGS_RE.search(text)
        if not name:
            return None
        return f"{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip(), True
    if not text and images:
        text = "[Image]"
    return (text, False) if text else None


class _HistoryBuilder:
    """Folds the active path into Whisper rows: one user row per prompt and
    one assistant row for everything the assistant did until the next one."""

    def __init__(self, results: dict[str, tuple[str, bool]]):
        self.rows: list[dict] = []
        self.results = results
        self._reply: dict | None = None
        # A slash command is kept only when the assistant answered it;
        # /model, /copy and the like run locally and get no reply.
        self._command: dict | None = None

    def prompt(self, text: str, command: bool, timestamp: str) -> None:
        self._reply = None
        row = {"role": "user", "content": text, "timestamp": timestamp}
        if command:
            self._command = row
            return
        self._command = None
        self.rows.append(row)

    def _reply_row(self, timestamp: str) -> dict:
        if self._command is not None:
            self.rows.append(self._command)
            self._command = None
        if self._reply is None:
            # Stamped when the reply starts, as a live turn stamps it.
            self._reply = {"role": "assistant", "content": "", "timestamp": timestamp}
            self.rows.append(self._reply)
        return self._reply

    def assistant(self, blocks: list, timestamp: str) -> None:
        row = self._reply_row(timestamp)
        for b in blocks:
            if not isinstance(b, dict):
                continue
            kind = b.get("type")
            if kind == "text" and (b.get("text") or "").strip():
                row["content"] = _join(row["content"], b["text"].strip())
            elif kind == "thinking" and (b.get("thinking") or "").strip():
                row["_thinkingText"] = _join(row.get("_thinkingText", ""), b["thinking"].strip())
            elif kind == "tool_use":
                row.setdefault("toolUse", []).append(self._tool_use(b))

    def _tool_use(self, block: dict) -> dict:
        tool_id = block.get("id") or ""
        entry = {
            "toolId": tool_id,
            "toolName": block.get("name") or "tool",
            "input": block.get("input") if isinstance(block.get("input"), dict) else {},
        }
        if tool_id in self.results:
            result, is_error = self.results[tool_id]
            entry["result"] = result
            entry["status"] = "error" if is_error else "complete"
        else:
            entry["status"] = "stopped"
        return entry

    def interrupted(self, timestamp: str) -> None:
        row = self._reply_row(timestamp)
        row["content"] = _join(row["content"], _STOPPED)
        row["stopped"] = True
        self._reply = None


def _join(head: str, tail: str) -> str:
    return f"{head}\n\n{tail}" if head else tail


def _title(entries: list[dict], rows: list[dict]) -> str:
    """The name the session was given (renamed, then generated), else the
    first prompt."""
    for kind, field in (
        ("custom-title", "customTitle"),
        ("ai-title", "aiTitle"),
        ("summary", "summary"),
    ):
        names = [
            e[field].strip()
            for e in entries
            if e.get("type") == kind and isinstance(e.get(field), str)
        ]
        names = [n for n in names if n]
        if names:
            return names[-1]
    for row in rows:
        if row["role"] == "user":
            first_line = row["content"].strip().splitlines()[0] if row["content"].strip() else ""
            if first_line:
                return first_line[:_TITLE_FROM_PROMPT_CHARS]
    return FALLBACK_TITLE


def parse(raw: str) -> tuple[str, list[dict]]:
    """``(title, chat_history)`` for a Claude Code transcript. Raises
    ValueError with a user-facing message when it holds no conversation."""
    entries = [obj for obj in map(_load, raw.splitlines()) if obj is not None]
    messages = [e for e in entries if _is_message(e) and e.get("uuid")]
    # A subagent's own transcript is all sidechain; it stands on its own.
    main = [e for e in messages if not e.get("isSidechain")] or messages
    if not main:
        raise ValueError("this Claude Code transcript has no messages")
    path = _active_path(entries, main[-1])
    builder = _HistoryBuilder(_tool_results(main))

    for entry in entries:
        if entry.get("uuid") not in path:
            continue
        timestamp = entry.get("timestamp") or ""
        if entry.get("type") == "attachment":
            attachment = entry.get("attachment") or {}
            if attachment.get("type") == "queued_command":
                typed = _typed_text(attachment.get("prompt"))
                if typed:
                    builder.prompt(*typed, timestamp=timestamp)
            continue
        if not _is_message(entry) or entry.get("isMeta") or entry.get("isCompactSummary"):
            continue
        content = entry["message"].get("content")
        if entry["type"] == "assistant":
            # Synthetic replies are the harness talking; an API error or a
            # usage limit notice is one the user saw, so it stays.
            if entry["message"].get("model") == "<synthetic>" and not entry.get(
                "isApiErrorMessage"
            ):
                continue
            builder.assistant(content if isinstance(content, list) else [], timestamp)
            continue
        if entry.get("isVisibleInTranscriptOnly"):
            continue
        typed = _typed_text(content)
        if typed is None:
            continue
        if typed[0].startswith(_INTERRUPTED):
            builder.interrupted(timestamp)
            continue
        builder.prompt(*typed, timestamp=timestamp)

    rows = builder.rows
    if not rows:
        raise ValueError("this Claude Code transcript has no conversation to import")
    return _title(entries, rows), rows
