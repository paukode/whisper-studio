"""Whether what a reply claims actually happened.

A claim (server/goals/claims.py) is checked against the world, never against
the model's word. A file must be on disk and not empty, and written since the
turn began when the reply says it was made (a copy that kept its old times
counts when a successful call of the turn named it); a folder must hold
something, new this turn when the reply says it wrote there; a removed file
must be gone; an artifact card must have been made this turn or be one the
session holds. An act that leaves nothing on this disk (a push, a commit, a
pull request, a merge, an issue, an upload, a message, a deploy, a schedule,
a memory, a setting, a link) must match a tool call of this turn that did it
and succeeded, naming the same target when the claim names one. A call that
is still waiting for the user's approval did nothing yet.

A claim that points back to an earlier turn needs only its file to exist, or
its link to appear earlier in the conversation; an earlier act cannot be seen
from here and is let through unchecked. In plan mode only a path that a
writing call of the turn named is checked (deliverables.targeted_paths): any
other is a file the plan will write.

Pure functions over the provider-neutral message list, plus stat.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from server.goals import claims as c
from server.goals.deliverables import call_input, resolve_path, targeted_paths, turn_messages
from server.goals.tail import _render_blocks

# Filesystem times are coarse and a tool may stamp a file a moment before the
# turn's clock started.
_FRESH_SLACK_S = 2.0
# A folder the reply says it wrote to is read this far, newest first.
_MAX_ENTRIES = 5000
_ARTIFACT_TOOLS = ("create_artifact", "edit_artifact")
_COMMAND_TOOLS = frozenset(
    {"ws_run_command", "terminal_run", "terminal_send", "run_python", "aws_cli", "run_tool_script"}
)

# What a failed or not yet run call returns: the executor's own markers
# (tool_executor, the loop guard, the approval card, and the result an
# approval card sends back when the user denied the action or it failed), an
# error or traceback, or a JSON error body.
_FAILED_RESULT_RE = re.compile(
    r"^\s*(?:\[(?:tool error|denied|skipped|refused|blocked|ws_approval|error|failed|cancel"
    r"|user denied|user approved but)"
    r"|(?:error|failed|failure|blocked|refused|denied)\b|[\w ]{0,40}\berror:"
    r"|traceback \(most recent call last\)|\{\s*\"(?:error|errors)\")",
    re.IGNORECASE,
)
# A command's own exit status: terminal_run's first line, run_python's and
# ws_run_command's "(exit code N".
_EXIT_RE = re.compile(r"^exit_code:\s*(-?\d+)|\(exit code (-?\d+)", re.MULTILINE)
# A command that printed how it failed, where no exit status was given.
_COMMAND_FAILED_RE = re.compile(
    r"^(?:fatal|error):|! \[(?:rejected|remote rejected)\]|\bupload failed:"
    r"|\bAn error occurred \(|Traceback \(most recent call last\)|: command not found"
    r"|\bPermission denied\b",
    re.MULTILINE,
)

# Which calls do each act: a tool name, or text in the call's arguments (a
# shell command, a script, an API path). MCP tools match by their names.
_ACTS: dict[str, tuple[re.Pattern | None, re.Pattern | None]] = {
    c.PUSH: (re.compile(r"^git_push(?:_pr)?$"), re.compile(r"\bgit\s+push\b")),
    c.COMMIT: (
        re.compile(r"^git_(?:add_)?commit$|commit", re.IGNORECASE),
        re.compile(r"\bgit\s+commit\b"),
    ),
    c.PR: (
        re.compile(r"^git_push_pr$|pull_?request|create_pr\b", re.IGNORECASE),
        re.compile(r"\bgh\s+pr\s+create\b|/pulls\b"),
    ),
    c.MERGE: (
        re.compile(r"^git_merge$|^ws_merge_worktree$|merge", re.IGNORECASE),
        re.compile(r"\bgh\s+pr\s+merge\b|\bgit\s+merge\b|/merge\b"),
    ),
    c.ISSUE: (
        re.compile(r"issue", re.IGNORECASE),
        re.compile(r"\bgh\s+issue\s+create\b|/issues\b"),
    ),
    c.UPLOAD: (
        re.compile(r"upload|put_object", re.IGNORECASE),
        re.compile(
            r"\baws\s+s3\s+(?:cp|sync|mv)\b|\bs3api\s+put-object\b|\bupload_file(?:obj)?\b"
            r"|\bput_object\b|\bgsutil\s+cp\b|\brclone\s+(?:copy|sync)\b|\bscp\b"
        ),
    ),
    c.MESSAGE: (
        re.compile(
            r"send|e?mail|message|post|reply|slack|chat|notif|sms|invite|comment|discord|teams"
            r"|telegram",
            re.IGNORECASE,
        ),
        re.compile(r"\bsendmail\b|\bmail\s+-s\b|\bosascript\b.*\bMail\b"),
    ),
    c.PUBLISH: (
        re.compile(r"deploy|publish|release", re.IGNORECASE),
        re.compile(
            r"\bdeploy\b|\bpublish\b|\bgh\s+release\s+create\b|\bnpm\s+publish\b|\btwine\s+upload\b"
            r"|\bvercel\b|\bnetlify\s+deploy\b|\bcdk\s+deploy\b|\bsam\s+deploy\b|\bfly\s+deploy\b"
            r"|\bfirebase\s+deploy\b|\bkubectl\s+apply\b|\bterraform\s+apply\b|/releases\b"
        ),
    ),
    c.SCHEDULE: (
        re.compile(r"cron|schedule|reminder", re.IGNORECASE),
        re.compile(r"\bcrontab\b|\blaunchctl\s+(?:load|bootstrap)\b"),
    ),
    c.MEMORY: (re.compile(r"^memory_write$|memory_(?:add|save|store)", re.IGNORECASE), None),
    c.SETTING: (re.compile(r"^config_set$|setting|preference", re.IGNORECASE), None),
    c.LINK: (None, None),
}
_ACT_LABELS = {
    c.PUSH: "Pushed",
    c.COMMIT: "Committed",
    c.PR: "Pull request",
    c.MERGE: "Merged",
    c.ISSUE: "Issue",
    c.UPLOAD: "Uploaded",
    c.MESSAGE: "Sent",
    c.PUBLISH: "Published",
    c.SCHEDULE: "Scheduled",
    c.LINK: "Link",
    c.MEMORY: "Saved to memory",
    c.SETTING: "Setting changed",
}
# How each act reads where it did not happen; {to} is " to `target`" or "".
_ACT_NOTES = {
    c.PUSH: "no push{to} succeeded in this turn",
    c.COMMIT: "no commit succeeded in this turn",
    c.PR: "no pull request was opened in this turn",
    c.MERGE: "no merge succeeded in this turn",
    c.ISSUE: "no issue was filed in this turn",
    c.UPLOAD: "no upload{to} succeeded in this turn",
    c.MESSAGE: "no message{to} was sent in this turn",
    c.PUBLISH: "no deploy or publish succeeded in this turn",
    c.SCHEDULE: "no task was scheduled in this turn",
    c.LINK: "nothing in this turn produced {target}",
    c.MEMORY: "nothing was saved to memory in this turn",
    c.SETTING: "no setting was changed in this turn",
}
# A tool that only reads is no evidence that anything was done: its name
# says so ("list_issues", "search_emails", "git_log"), or it is registered
# read-only (server/executors) or marked so by its MCP server.
_READ_VERB_RE = re.compile(
    r"(?:^|_)(?:get|list|search|read|fetch|find|query|view|describe|show|status|check|inspect"
    r"|lookup|count|diff|log|logs|blame|preview|history|info)(?:_|$)",
    re.IGNORECASE,
)
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*://(?:www\.)?", re.IGNORECASE)
_URL_IN_RE = re.compile(r"https?://[^\s\"'<>()\[\]`\\]+")


@dataclass(frozen=True)
class Call:
    """One tool call of the turn: its name, its arguments as text, what it
    returned, and whether it did what it was asked (``ok``)."""

    name: str
    text: str
    result: str
    ok: bool


@dataclass(frozen=True)
class Verdict:
    """What checking one claim found. ``checked`` False means it could not
    be checked here and is let through (a planned file, an earlier act).
    ``note`` says what is true instead, for the user and the model;
    ``receipt`` describes what was verified, for the chat's chips."""

    ok: bool
    checked: bool = True
    note: str = ""
    receipt: dict | None = None


@dataclass
class Evidence:
    """What a turn did, as the checks need it."""

    workspace: str | None = None
    started_at: float | None = None
    calls: list[Call] = field(default_factory=list)
    earlier_text: str = ""
    has_artifact: bool = False
    plan_targets: set[str] | None = None

    @classmethod
    def of(
        cls,
        messages: list,
        *,
        workspace: str | None = None,
        started_at: float | None = None,
        session_has_artifact: bool = False,
        plan_mode: bool = False,
    ) -> Evidence:
        turn = turn_messages(messages)
        calls = turn_calls(turn)
        return cls(
            workspace=workspace,
            started_at=started_at,
            calls=calls,
            earlier_text="\n".join(
                _render_blocks(m.get("content"))
                for m in messages[: len(messages) - len(turn)]
                if isinstance(m, dict)
            ),
            has_artifact=session_has_artifact
            or any(call.ok and call.name in _ARTIFACT_TOOLS for call in calls),
            plan_targets=targeted_paths(messages, workspace) if plan_mode else None,
        )


def _result_text(raw) -> str:
    if isinstance(raw, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in raw)
    return "" if raw is None else str(raw)


def succeeded(name: str, result: str) -> bool:
    """True when a call's result says it did what it was asked."""
    if _FAILED_RESULT_RE.match(result[:400]):
        return False
    codes = [int(a or b) for a, b in _EXIT_RE.findall(result)]
    if codes:
        return codes[-1] == 0
    return not (name in _COMMAND_TOOLS and _COMMAND_FAILED_RE.search(result))


def turn_calls(rows: list) -> list[Call]:
    """Every tool call among ``rows`` with what it returned (an Anthropic
    tool_use and its tool_result, or a Responses function_call and its
    function_call_output). A call with no result yet did nothing."""
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
            text = json.dumps(call_input(b), ensure_ascii=False, default=str)
            result, is_error = results.get(key, ("", True))
            ok = key in results and not is_error and succeeded(name, result)
            calls.append(Call(name, text, result, ok))
    return calls


# ── Receipts ──────────────────────────────────────────────────────────────


def _size(n: int) -> str:
    if n < 1000:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1000
        if n < 1000 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


def _when(ts: float) -> tuple[str, str]:
    at = datetime.fromtimestamp(ts).astimezone()
    return at.strftime("%H:%M"), at.isoformat(timespec="seconds")


def _path_receipt(kind: str, path: str, detail: str, ts: float | None = None) -> dict:
    receipt = {
        "kind": kind,
        "target": path,
        "label": os.path.basename(path.rstrip("/")) or path,
        "detail": detail,
    }
    if kind != c.REMOVED:
        receipt["href"] = f"#wsfile={quote(path)}&open=os"
    if ts is not None:
        receipt["at"] = _when(ts)[1]
    return receipt


def _act_receipt(claim: c.Claim, call: Call | None) -> dict:
    target = claim.target
    detail = f"to {target}" if target and claim.kind in (c.PUSH, c.UPLOAD, c.MESSAGE) else target
    receipt = {
        "kind": claim.kind,
        "target": target or (call.name if call else claim.kind),
        "label": _ACT_LABELS[claim.kind],
        "detail": detail,
    }
    url = target if _URL_IN_RE.fullmatch(target or "") else ""
    if not url and call is not None and claim.kind in (c.PR, c.ISSUE, c.PUBLISH):
        found = _URL_IN_RE.search(call.result)
        url = found.group(0).rstrip(".,;:") if found else ""
    if url:
        receipt["href"] = url
    return receipt


# ── Checks ────────────────────────────────────────────────────────────────


def _fresh(st: os.stat_result, started_at: float | None) -> bool:
    # The modification time only: the change time moves on any metadata
    # update (a tag, an extended attribute), which is not a write. A copy that
    # kept its old times is found by the call that made it instead.
    return started_at is None or st.st_mtime >= started_at - _FRESH_SLACK_S


def _named_by_a_call(ev: Evidence, target: str, path: str) -> bool:
    """True when a successful call of the turn names this path (as written,
    resolved, from the home folder, or within the workspace)."""
    home = os.path.expanduser("~")
    needles = {target, path}
    if path.startswith(home + os.sep):
        needles.add("~" + path[len(home) :])
    if ev.workspace and path.startswith(os.path.join(ev.workspace, "")):
        needles.add(os.path.relpath(path, ev.workspace))
    needles = {json.dumps(n, ensure_ascii=False)[1:-1] for n in needles if n}
    # A command may name its output by file name alone ("wget .../q3.pdf").
    name = os.path.basename(path)
    by_name = len(name) >= 5 and "." in name
    return any(
        call.ok
        and (
            any(n in call.text for n in needles)
            or (by_name and call.name in _COMMAND_TOOLS and name in call.text)
        )
        for call in ev.calls
    )


def _check_folder(claim: c.Claim, ev: Evidence, path: str, shown: str) -> Verdict:
    try:
        with os.scandir(path) as it:
            entries = list(itertools.islice(it, _MAX_ENTRIES))
    except OSError:
        return Verdict(False, note=f"{shown} cannot be read")
    if not entries:
        return Verdict(False, note=f"{shown} is empty")
    if not claim.made or claim.earlier or ev.started_at is None:
        return Verdict(True, receipt=_path_receipt(c.FOLDER, path, f"{len(entries)} items"))
    fresh: list[tuple[float, str]] = []
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if _fresh(st, ev.started_at):
            fresh.append((st.st_mtime, e.name))
    if not fresh:
        if _named_by_a_call(ev, claim.target, path):
            return Verdict(True, receipt=_path_receipt(c.FOLDER, path, "written in this turn"))
        return Verdict(False, note=f"nothing in {shown} was written in this turn")
    fresh.sort(reverse=True)
    newest = fresh[0][1]
    more = f" and {len(fresh) - 1} more" if len(fresh) > 1 else ""
    return Verdict(True, receipt=_path_receipt(c.FOLDER, path, f"{newest}{more}", fresh[0][0]))


def _check_path(claim: c.Claim, ev: Evidence) -> Verdict:
    target = claim.target
    if not claim.deliverable and not os.path.isabs(os.path.expanduser(target)) and not ev.workspace:
        # A relative path in prose with no workspace to read it against. A
        # document the app links to is still checked where it points.
        return Verdict(True, checked=False)
    path = resolve_path(target, ev.workspace)
    if ev.plan_targets is not None and path not in ev.plan_targets:
        return Verdict(True, checked=False)
    shown = f"`{target}`"
    if claim.kind == c.REMOVED:
        if os.path.lexists(path):
            return Verdict(False, note=f"{shown} still exists")
        return Verdict(True, receipt=_path_receipt(c.REMOVED, path, "removed"))
    try:
        st = os.stat(path)
    except OSError:
        return Verdict(False, note=f"{shown} does not exist")
    if stat.S_ISDIR(st.st_mode):
        return _check_folder(claim, ev, path, shown)
    if st.st_size == 0:
        return Verdict(False, note=f"{shown} is empty")
    if (
        claim.made
        and not claim.earlier
        and not _fresh(st, ev.started_at)
        and not _named_by_a_call(ev, target, path)
    ):
        return Verdict(False, note=f"{shown} was not written in this turn")
    clock, _iso = _when(st.st_mtime)
    return Verdict(
        True,
        receipt=_path_receipt(c.FILE, path, f"{_size(st.st_size)}, saved {clock}", st.st_mtime),
    )


def _flat(text: str) -> str:
    """Text as an address is compared: lower case, no scheme, no "www."."""
    return _SCHEME_RE.sub("", text.lower())


def _mentions(call: Call, kind: str, target: str) -> bool:
    """True when the call's arguments or result name the claim's target."""
    hay = _flat(f"{call.text}\n{call.result}")
    t = _flat(target).rstrip("/")
    if kind == c.PUSH:
        t = t.rsplit("/", 1)[-1]
    elif kind in (c.PR, c.ISSUE, c.MERGE) and t.startswith("#"):
        return bool(
            re.search(
                rf"(?:#|/(?:pull|pulls|issues|merge_requests)/|\bnumber\"?:\s*){t[1:]}\b", hay
            )
        )
    elif kind == c.MESSAGE:
        t = t.lstrip("@").split()[0] if t.strip() else t
    return bool(t) and re.search(rf"(?<![\w.-]){re.escape(t)}(?![\w-])", hay) is not None


def _link_known(target: str, ev: Evidence) -> bool:
    """True when a successful call of the turn, or the conversation before
    it, names this address."""
    t = _flat(target).rstrip("/")
    return any(call.ok and t in _flat(f"{call.text}\n{call.result}") for call in ev.calls) or (
        t in _flat(ev.earlier_text)
    )


def _can_act(name: str) -> bool:
    """False for a tool that only reads (see _READ_VERB_RE)."""
    if _READ_VERB_RE.search(name.rsplit("__", 1)[-1]):
        return False
    from server.executors import is_read_only

    if is_read_only(name):
        return False
    if name.startswith("mcp__"):
        from server.mcp_read_only import is_read_only_mcp_tool

        try:
            return not is_read_only_mcp_tool(name)
        except Exception:  # noqa: BLE001 - a registry fault must not hide an act
            return True
    return True


def _check_act(claim: c.Claim, ev: Evidence) -> Verdict:
    names, texts = _ACTS[claim.kind]
    if claim.kind == c.LINK or (claim.target and _URL_IN_RE.match(claim.target)):
        if _link_known(claim.target, ev):
            return Verdict(True, receipt=_act_receipt(claim, None))
        if claim.kind == c.LINK:
            return (
                Verdict(True, checked=False)
                if claim.earlier
                else Verdict(False, note=_ACT_NOTES[c.LINK].format(target=f"`{claim.target}`"))
            )
    doing = [
        call
        for call in ev.calls
        if call.ok
        and _can_act(call.name)
        and ((names is not None and names.search(call.name)) or (texts and texts.search(call.text)))
    ]
    if claim.target and claim.kind not in (c.SCHEDULE, c.MEMORY, c.SETTING):
        doing = [call for call in doing if _mentions(call, claim.kind, claim.target)]
    if doing:
        return Verdict(True, receipt=_act_receipt(claim, doing[-1]))
    if claim.earlier:
        return Verdict(True, checked=False)
    to = f" to `{claim.target}`" if claim.target else ""
    return Verdict(False, note=_ACT_NOTES[claim.kind].format(to=to, target=claim.target))


def check(claim: c.Claim, ev: Evidence) -> Verdict:
    """Whether ``claim`` holds, by the rules in the module notes."""
    if claim.kind in (c.FILE, c.FOLDER, c.REMOVED):
        return _check_path(claim, ev)
    if claim.kind == c.ARTIFACT:
        if ev.has_artifact:
            return Verdict(
                True, receipt={"kind": c.ARTIFACT, "target": "artifact", "label": "Artifact card"}
            )
        return Verdict(False, note="no artifact card was made")
    return _check_act(claim, ev)


# How a claim that did not hold reads to the user, ahead of its note.
_NOT_DONE = {
    c.FILE: "Not saved",
    c.FOLDER: "Not saved",
    c.REMOVED: "Not removed",
    c.ARTIFACT: "No artifact card",
    c.PUSH: "Not pushed",
    c.COMMIT: "Not committed",
    c.PR: "No pull request",
    c.MERGE: "Not merged",
    c.ISSUE: "No issue filed",
    c.UPLOAD: "Not uploaded",
    c.MESSAGE: "Not sent",
    c.PUBLISH: "Not published",
    c.SCHEDULE: "Not scheduled",
    c.LINK: "Not verified",
    c.MEMORY: "Not saved to memory",
    c.SETTING: "Not changed",
}


def not_done(claim: c.Claim, verdict: Verdict) -> str:
    """One sentence for the user where a claim did not hold, such as "Not
    saved: `~/a.docx` does not exist." """
    return f"{_NOT_DONE[claim.kind]}: {verdict.note}."
