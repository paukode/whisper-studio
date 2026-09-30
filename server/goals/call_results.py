"""What each tool call of a turn returned, as the claim check reads it.

A call did what it was asked only when its result says so: not a failure
marker of the app's own, an error, a refusal or a denial, not a non-zero
exit, not an error body. A call that has not finished did nothing yet: a
command moved to the background, a terminal still waiting for input or past
its time limit. A background command counts from the moment a later
task_status or task_output of the turn shows it finished with exit 0.

The stream guard keeps each call as it first saw it (``merge_calls``): a
tool result that mid-turn compaction shortened later ("...[truncated]")
must not turn a failed push into a pushed one, nor the reverse.

Agent reports are claims checked where they were made (each sub-agent's own
guard held what it could not verify), so what they report is evidence too:
a sub-agent's output returned to this turn, an agent_report row, a report
that arrived mid-turn.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from server.goals.acts import Act, acts_of, writes_of
from server.goals.deliverables import call_input

_COMMAND_TOOLS = frozenset(
    {"ws_run_command", "terminal_run", "terminal_send", "run_python", "aws_cli", "run_tool_script"}
)
# What a failed, refused or not yet finished call returns: a bracketed marker
# of the app's own that says so ("[Tool Error]", "[MCP Error]", "[Hook
# denied]", "[Plan Mode]", "[Loop guard]", "[Stopped by user]", an approval
# still pending or denied), an error or a traceback. "[Hook context]" and a
# loop-guard notice after a result are not failures.
_FAILED_RESULT_RE = re.compile(
    r"^\s*(?:\[(?:[^\]\n]{0,40}\b(?:error|denied|blocked|refused|skipped|stopped|failed|failure"
    r"|cancel\w*)\b[^\]\n]{0,40}|ws_approval|mcp|plan mode|loop guard|user denied"
    r"|user approved but[^\]\n]*)\]"
    r"|(?:error|failed|failure|blocked|refused|denied)\b|[\w ]{0,40}\berror:"
    r"|http\s+[45]\d\d\b|traceback \(most recent call last\))",
    re.IGNORECASE,
)
# A result that opens by saying it did not happen ("Unknown memory tool: x",
# "File not found: x", "No workspace connected.", "nothing to commit").
_FAILED_OPENING_RE = re.compile(
    r"^\s*(?:unknown\b|no\s+(?:workspace|command|file|such|matching|permission|access)\b"
    r"|(?:file|worktree|tool|command|branch|path|session|agent|server|repo\w*)\b[^\n]{0,60}"
    r"\bnot\s+(?:found|enabled|available|connected)\b|not\s+found\b"
    r"|nothing\s+to\s+(?:commit|push|merge)\b|could\s+not\b|couldn.t\b|cannot\b|can.t\b"
    r"|unable\s+to\b|invalid\b|missing\b|[\w ]{0,30}\berror\b"
    r"|warn(?:ing)?:[^\n]*\b(?:rejected|failed|not\s+sent|bounced)\b)",
    re.IGNORECASE,
)
# A first line that says the thing was not done ("Agent a1 is not running;
# message not delivered", "1 recipient rejected").
_NOT_DONE_LINE_RE = re.compile(
    r"\bnot\s+(?:delivered|sent|created|merged|pushed|published|uploaded|posted|scheduled"
    r"|saved|deployed)\b|\bfailed\s+to\b|\brecipients?\s+rejected\b|\b(?:was|were)\s+rejected\b"
    r"|\baccess\s+denied\b|\bforbidden\b|\bunauthori[sz]ed\b",
    re.IGNORECASE,
)
# Tools whose success says so in words of its own: nothing else counts.
_SUCCESS_SAYS = {
    "memory_write": re.compile(r"^\s*Memory file saved\b"),
    "ws_merge_worktree": re.compile(r'"merged"\s*:\s*true'),
}
# A command's own exit status: terminal_run's first line, run_python's and
# ws_run_command's "(exit code N".
_EXIT_RE = re.compile(r"^exit_code:\s*(-?\d+)|\(exit code (-?\d+)", re.MULTILINE)
# A command that printed how it failed, where no exit status was given.
_COMMAND_FAILED_RE = re.compile(
    r"^(?:fatal|error):|! \[(?:rejected|remote rejected)\]|\bupload failed:"
    r"|\bAn error occurred \(|Traceback \(most recent call last\)|: command not found"
    r"|\bPermission denied\b|^npm (?:ERR!|error)\s|^(?:unauthorized|denied):",
    re.MULTILINE,
)
# A call that has not finished: moved to the background, a terminal program
# still waiting for input or still printing, a command past its time limit.
_PENDING_RE = re.compile(
    r"^\[Background Task Started\]|^wait_reason:\s*(?:stdin_read|timeout)\b"
    r"|^TIMED OUT after\b|^exit_code:\s*None\b",
    re.MULTILINE,
)
_TASK_ID_RE = re.compile(r"^\[Background Task Started\] task_id=(\S+)", re.MULTILINE)
_TASK_OUTPUT_HEAD_RE = re.compile(
    r"^\[(\w+) task (\S+) \N{EM DASH} (\w+)(?:, exit (-?\d+))?\]", re.MULTILINE
)
# The rows that carry agent reports into a turn (sessions._agent_report_prompt_view
# and runner._midturn_text).
_REPORT_ROW_PREFIXES = (
    '[Agent reports from "',
    "<system-reminder>Reports from agents launched in this conversation",
)


@dataclass(frozen=True, eq=False)
class Call:
    """One tool call of the turn: its id, name and arguments (also as text),
    what it returned, whether it did what it was asked (``ok``) or has not
    finished yet (``pending``), and what it did (``acts``) and wrote
    (``writes``), whether or not it succeeded."""

    id: str
    name: str
    input: dict
    text: str
    result: str
    ok: bool
    acts: tuple[Act, ...]
    writes: tuple[str, ...]
    pending: bool = False


def _result_text(raw) -> str:
    if isinstance(raw, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in raw)
    return "" if raw is None else str(raw)


def _json_failed(result: str) -> bool:
    body = result.lstrip()
    if not body.startswith("{"):
        return False
    try:
        data = json.loads(body)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("ok") is False or data.get("success") is False or data.get("error"):
        return True
    if data.get("isError") is True or (data.get("errors") and not data.get("data")):
        return True
    # A GitHub error body: a message and its documentation link, nothing made.
    if data.get("message") and data.get("documentation_url"):
        if not any(data.get(k) for k in ("id", "number", "html_url", "url", "sha")):
            return True
    status = str(data.get("status") or "").lower()
    return status in ("error", "failed", "failure") or (status.isdigit() and int(status) >= 400)


def still_running(result: str) -> bool:
    """True when a call's result says it has not finished (see
    _PENDING_RE)."""
    return bool(_PENDING_RE.search(result[:4000]))


def succeeded(name: str, result: str) -> bool:
    """True when a call's result says it did what it was asked."""
    head = result[:400]
    if _FAILED_RESULT_RE.match(head) or _FAILED_OPENING_RE.match(head):
        return False
    if still_running(result):
        return False
    first = result.lstrip().split("\n", 1)[0][:300]
    if _NOT_DONE_LINE_RE.search(first):
        return False
    says = _SUCCESS_SAYS.get(name)
    if says is not None and not head.startswith("[User approved]"):
        return bool(says.search(result))
    if _json_failed(result):
        return False
    codes = [int(a or b) for a, b in _EXIT_RE.findall(result)]
    if codes:
        return codes[-1] == 0
    return not (name in _COMMAND_TOOLS and _COMMAND_FAILED_RE.search(result))


def _finished(name: str, given: dict, result: str) -> tuple[str, bool] | None:
    """What a task_status or task_output call says about the background
    task it names: (its id, whether it finished with exit 0), or None while
    it runs or when the call is about something else."""
    if name == "task_output":
        head = _TASK_OUTPUT_HEAD_RE.search(result)
        if not head or head.group(3) == "running":
            return None
        code = head.group(4)
        return head.group(2), head.group(3) == "completed" and (code is None or code == "0")
    if name == "task_status":
        try:
            data = json.loads(result)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("status") in (None, "running", "pending"):
            return None
        task = str(data.get("task_id") or given.get("task_id") or "")
        ok = data.get("status") == "completed" and data.get("exit_code") in (0, None)
        return (task, ok) if task else None
    return None


def turn_calls(rows: list, cache: dict | None = None) -> list[Call]:
    """Every tool call among ``rows`` with what it returned (an Anthropic
    tool_use and its tool_result, or a Responses function_call and its
    function_call_output). A call with no result yet did nothing. ``cache``
    keeps each call's arguments as text across calls, by id. A background
    command that a later task_status or task_output shows finished with exit
    0 did what it was asked, and what that later call printed is part of its
    result."""
    results: dict[str, tuple[str, bool]] = {}
    for m in rows:
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        for b in m["content"]:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                results[str(b.get("tool_use_id"))] = (
                    _result_text(b.get("content")),
                    bool(b.get("is_error")),
                )
            elif b.get("type") == "function_call_output":
                results[str(b.get("call_id"))] = (_result_text(b.get("output")), False)
    calls: list[Call] = []
    for m in rows:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for b in m.get("content") if isinstance(m.get("content"), list) else []:
            if not isinstance(b, dict) or b.get("type") not in ("tool_use", "function_call"):
                continue
            key = str(
                b.get("id") if b.get("type") == "tool_use" else b.get("call_id") or b.get("id")
            )
            name = str(b.get("name") or "")
            given = call_input(b)
            text = cache.get(key) if cache is not None else None
            if text is None:
                text = json.dumps(given, ensure_ascii=False, default=str)
                if cache is not None:
                    cache[key] = text
            result, is_error = results.get(key, ("", True))
            ok = key in results and not is_error and succeeded(name, result)
            calls.append(
                Call(
                    id=key,
                    name=name,
                    input=given,
                    text=text,
                    result=result,
                    ok=ok,
                    acts=tuple(acts_of(name, given, text, result)),
                    writes=tuple(writes_of(name, given)),
                    pending=key in results and not is_error and still_running(result),
                )
            )
    return _settle_background(calls)


def _settle_background(calls: list[Call]) -> list[Call]:
    """Each background command a later call of the list reports finished,
    done (exit 0) or failed."""
    started = {m.group(1): i for i, x in enumerate(calls) if (m := _TASK_ID_RE.search(x.result))}
    if not started:
        return calls
    out = list(calls)
    for later in calls:
        found = _finished(later.name, later.input, later.result)
        if found is None or found[0] not in started:
            continue
        i = started[found[0]]
        x = out[i]
        result = f"{x.result}\n{later.result}"
        out[i] = replace(
            x,
            result=result,
            ok=found[1],
            pending=False,
            acts=tuple(acts_of(x.name, x.input, x.text, result)),
        )
    return out


def merge_calls(kept: dict[str, Call], seen: list[Call]) -> None:
    """Fold the calls just read (``seen``) into those kept for the turn, by
    id: a call is kept as it was first read with a result, since compaction
    may shorten that result later; a call first read before its result came
    back takes the one it has now."""
    for x in seen:
        old = kept.get(x.id)
        if old is None or (not old.result and x.result) or (old.pending and not x.pending):
            kept[x.id] = x


# ── agent reports ─────────────────────────────────────────────────────────


def _report_of(x: Call) -> str:
    """The report a call of the turn returned from an agent, or ""."""
    if x.name == "spawn_agent":
        try:
            data = json.loads(x.result)
        except ValueError:
            return ""
        return str(data.get("output") or "") if isinstance(data, dict) else ""
    if x.name == "task_output":
        head = _TASK_OUTPUT_HEAD_RE.search(x.result)
        if head and head.group(1) == "agent":
            return x.result[head.end() :]
    return ""


def report_texts(calls: list[Call], rows: list) -> list[str]:
    """The agent reports the turn read: returned by its calls, or carried in
    by ``rows`` (an agent_report row, a report that arrived mid-turn)."""
    texts = [t for x in calls if (t := _report_of(x))]
    for m in rows:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        blocks = (
            [content] if isinstance(content, str) else content if isinstance(content, list) else []
        )
        for b in blocks:
            text = (
                b if isinstance(b, str) else str(b.get("text", "")) if isinstance(b, dict) else ""
            )
            for prefix in _REPORT_ROW_PREFIXES:
                at = text.find(prefix)
                if at >= 0:
                    texts.append(text[at:])
    return texts
