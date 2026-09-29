"""Whether what a reply claims actually happened.

A claim (server/goals/claims.py) is checked against the world, never against
the model's word:

- a file must be on disk and not empty, and where the reply says it was
  made, written since the turn began (a copy that kept its old times counts
  when a successful call of the turn wrote it); a folder must hold something,
  new this turn where the reply says it wrote there; a removed file must be
  gone, and removed by a call of the turn; an artifact card must have been
  made this turn or be one the session holds;
- every other act (a push, a commit, a pull request, a merge, an issue, an
  upload, a message, a deploy, a schedule, a memory, a setting) must be one
  that a successful call of the turn did (server/goals/acts.py reads what each
  call did), naming the claim's target where the claim names one; a link must
  come from a call that did something, or from earlier in the conversation.
  A call that failed, was refused or denied, still waits for approval, or
  still runs in the background did nothing.

A claim that restates one made earlier in the conversation or in a sub-agent's
report, about something this turn did not try again, is a recap: its file
needs only to exist, and its act is let through, since the earlier check held
it then. A claim the reader cannot tell from a description (a passive, a row
of a table of changes: ``Claim.firm`` False) is checked only when the turn
tried that act or wrote that file. In plan mode a path is checked where a
writing call of the turn named it, or where the reply says outright that it
made the file.

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
from functools import cached_property
from urllib.parse import quote

from server.goals import claims as c
from server.goals.acts import Act, acts_of, reads_only, writes_of
from server.goals.deliverables import call_input, resolve_path, targeted_paths, turn_messages
from server.goals.tail import _render_blocks

# Filesystem times are coarse and a tool may stamp a file a moment before the
# turn's clock started.
_FRESH_SLACK_S = 2.0
# A folder the reply says it wrote to is read this far.
_MAX_ENTRIES = 5000
_COMMAND_TOOLS = frozenset(
    {"ws_run_command", "terminal_run", "terminal_send", "run_python", "aws_cli", "run_tool_script"}
)
# A sub-agent's report went through its own claim check.
_REPORT_TOOLS = frozenset({"spawn_agent", "send_message"})
_BACKGROUND_MARK = "[Background Task Started]"

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
    r"|traceback \(most recent call last\))",
    re.IGNORECASE,
)
# A result that opens by saying it did not happen ("Unknown memory tool: x",
# "File not found: x", "No workspace connected.", "nothing to commit").
_FAILED_OPENING_RE = re.compile(
    r"^\s*(?:unknown\b|no\s+(?:workspace|command|file|such|matching|permission|access)\b"
    r"|(?:file|worktree|tool|command|branch|path|session|agent|server|repo\w*)\b[^\n]{0,60}"
    r"\bnot\s+(?:found|enabled|available|connected)\b|not\s+found\b"
    r"|nothing\s+to\s+(?:commit|push|merge)\b|could\s+not\b|couldn.t\b|cannot\b|can.t\b"
    r"|unable\s+to\b|invalid\b|missing\b|[\w ]{0,30}\berror\b)",
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
    r"|\bPermission denied\b",
    re.MULTILINE,
)
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
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*://(?:www\.)?", re.IGNORECASE)
_URL_IN_RE = re.compile(r"https?://[^\s\"'<>()\[\]`\\]+")
_HEX_RUN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_VERSION_ONLY_RE = re.compile(r"v?\d+(?:\.\d+)+")


@dataclass(frozen=True, eq=False)
class Call:
    """One tool call of the turn: its id, name and arguments (also as text),
    what it returned, whether it did what it was asked (``ok``), and what it
    did (``acts``) and wrote (``writes``), whether or not it succeeded."""

    id: str
    name: str
    input: dict
    text: str
    result: str
    ok: bool
    acts: tuple[Act, ...]
    writes: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """What checking one claim found. ``checked`` False means it could not
    be checked here and is let through (a planned file, an earlier act, a
    description). ``note`` says what is true instead, for the user and the
    model; ``receipt`` describes what was verified, for the chat's chips."""

    ok: bool
    checked: bool = True
    note: str = ""
    receipt: dict | None = None


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
    if data.get("errors") and not data.get("data"):
        return True
    return str(data.get("status") or "").lower() in ("error", "failed", "failure")


def succeeded(name: str, result: str) -> bool:
    """True when a call's result says it did what it was asked."""
    head = result[:400]
    if _FAILED_RESULT_RE.match(head) or _FAILED_OPENING_RE.match(head):
        return False
    if _BACKGROUND_MARK in result[:200]:
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


def turn_calls(rows: list, cache: dict | None = None) -> list[Call]:
    """Every tool call among ``rows`` with what it returned (an Anthropic
    tool_use and its tool_result, or a Responses function_call and its
    function_call_output). A call with no result yet did nothing. ``cache``
    keeps each call's arguments as text across calls, by id."""
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
                )
            )
    return calls


def _flat(text: str) -> str:
    """Text as an address is compared: lower case, no scheme, no "www."."""
    return _SCHEME_RE.sub("", text.lower())


def word_in(needle: str, hay: str) -> bool:
    return (
        bool(needle)
        and re.search(rf"(?<![\w.-]){re.escape(needle)}(?![\w-])", hay, re.IGNORECASE) is not None
    )


def _url_in(url: str, hay: str) -> bool:
    """True when ``hay`` names this exact address (".../pull/1" is not
    ".../pull/12")."""
    want = _flat(url).rstrip("/")
    return any(_flat(u).rstrip("/.,;:") == want for u in _URL_IN_RE.findall(hay))


@dataclass
class Evidence:
    """What a turn did, as the checks need it."""

    workspace: str | None = None
    started_at: float | None = None
    calls: list[Call] = field(default_factory=list)
    earlier_text: str = ""
    said_before: list[str] = field(default_factory=list)
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
        extra_calls: list[Call] | None = None,
        cache: dict | None = None,
    ) -> Evidence:
        """The evidence of the turn ``messages`` ends on. ``extra_calls``
        are calls of the turn the messages no longer hold (compaction, a
        salvage round), kept by the stream guard."""
        turn = turn_messages(messages)
        calls = turn_calls(turn, cache)
        if extra_calls:
            have = {x.id for x in calls}
            calls = [x for x in extra_calls if x.id not in have] + calls
        earlier = [m for m in messages[: len(messages) - len(turn)] if isinstance(m, dict)]
        return cls(
            workspace=workspace,
            started_at=started_at,
            calls=calls,
            earlier_text="\n".join(_render_blocks(m.get("content")) for m in earlier),
            said_before=[
                _render_blocks(m.get("content")) for m in earlier if m.get("role") == "assistant"
            ],
            has_artifact=session_has_artifact
            or any(x.ok and a.kind == c.ARTIFACT for x in calls for a in x.acts),
            plan_targets=targeted_paths(messages, workspace) if plan_mode else None,
        )

    # ── what the turn did and tried ──────────────────────────────────────

    def path(self, target: str) -> str:
        return resolve_path(target, self.workspace)

    @cached_property
    def done(self) -> list[Act]:
        return [a for x in self.calls if x.ok for a in x.acts]

    @cached_property
    def tried(self) -> set[str]:
        return {a.kind for x in self.calls for a in x.acts}

    @cached_property
    def written(self) -> list[tuple[str, Call]]:
        return [(self.path(p), x) for x in self.calls if x.ok for p in x.writes]

    @cached_property
    def tried_paths(self) -> set[str]:
        out = {self.path(p) for x in self.calls for p in x.writes}
        out |= {self.path(a.target) for x in self.calls for a in x.acts if a.kind == c.REMOVED}
        return out

    @cached_property
    def earlier_claims(self) -> list[c.Claim]:
        """The claims the conversation made before this turn, and those of the
        sub-agents' reports this turn: they were held to the check then."""
        texts = list(self.said_before)
        texts += [x.result for x in self.calls if x.ok and x.name in _REPORT_TOOLS]
        return [cl for text in texts for cl in c.read_claims(text)]

    def wrote(self, path: str, *, under: bool = False) -> bool:
        """True when a successful call of the turn wrote ``path`` (or, with
        ``under``, something inside it). A command may name its output by
        file name alone ("wget -O q3.pdf")."""
        name = os.path.basename(path)
        folder = path.rstrip("/") + "/"
        for written, call in self.written:
            if written == path or (under and written.startswith(folder)):
                return True
            if call.name in _COMMAND_TOOLS and len(name) >= 5 and "." in name:
                if os.path.basename(written) == name:
                    return True
        return False

    def removed(self, path: str) -> bool:
        for x in self.calls:
            if not x.ok:
                continue
            for a in x.acts:
                if a.kind != c.REMOVED or not a.target:
                    continue
                gone = self.path(a.target).rstrip("/")
                if path == gone or path.startswith(gone + "/"):
                    return True
        return False

    def said_before_path(self, path: str) -> bool:
        return any(
            e.kind in (c.FILE, c.FOLDER) and self.path(e.target) == path
            for e in self.earlier_claims
        )

    def said_before_act(self, claim: c.Claim) -> bool:
        return any(
            e.kind == claim.kind
            and (not claim.target or not e.target or _same_target(claim.target, e.target))
            for e in self.earlier_claims
        )

    def link_made(self, url: str) -> bool:
        """True when a successful call that did more than read names this
        address."""
        return any(
            x.ok and not reads_only(x.name) and _url_in(url, f"{x.text}\n{x.result}")
            for x in self.calls
        )


# ── matching a claim's target ─────────────────────────────────────────────


def _same_target(a: str, b: str) -> bool:
    def norm(t: str) -> str:
        t = _flat(t).strip().strip("`'\"").lstrip("#@").rstrip("/")
        return t.removeprefix("origin/")

    return norm(a) == norm(b)


def _target_matches(kind: str, target: str, act: Act) -> bool:
    """True when the act did what the claim names (see the module notes)."""
    want = target.strip()
    if not want:
        return True
    have = act.target.strip()
    text = act.text
    if kind == c.PUSH:
        branch = want.rsplit("/", 1)[-1]
        if have and have.rsplit("/", 1)[-1].lower() == branch.lower():
            return True
        return word_in(branch, text)
    if want.startswith("#") and want[1:].isdigit():
        n = want[1:]
        if have.startswith("#"):
            return have[1:] == n
        return bool(
            re.search(rf"(?:#|/(?:pull|pulls|issues|merge_requests)/|\"number\"\s*:\s*){n}\b", text)
        )
    if _URL_IN_RE.match(want):
        return _url_in(want, f"{have}\n{text}")
    if kind == c.UPLOAD:
        goal = want.rstrip("/")
        dest = have.rstrip("/")
        if dest:
            return goal == dest or goal.startswith(dest + "/") or dest.startswith(goal + "/")
        m = re.match(r"s3://([^/]+)/?(.*)", goal)
        return bool(m) and word_in(m.group(1), text) and (not m.group(2) or m.group(2) in text)
    if kind == c.MESSAGE:
        words = [w for w in re.split(r"[\s.@#_,-]+", want.lower()) if len(w) >= 3]
        hay = f"{have}\n{text}".lower()
        return not words or any(w in hay for w in words)
    if kind == c.PUBLISH and _VERSION_ONLY_RE.fullmatch(want):
        v = want.lower().lstrip("v")
        return bool(re.search(rf"(?<![\d.])v?{re.escape(v)}(?!\.?\d)", f"{have}\n{text}".lower()))
    if kind == c.COMMIT:
        sha = want.lower()
        return any(
            x.startswith(sha) or sha.startswith(x) for x in _HEX_RUN_RE.findall(text.lower())
        )
    return _same_target(want, have) or word_in(want, text)


# ── receipts ──────────────────────────────────────────────────────────────


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


def _act_receipt(claim: c.Claim, act: Act | None) -> dict:
    target = claim.target
    detail = f"to {target}" if target and claim.kind in (c.PUSH, c.UPLOAD, c.MESSAGE) else target
    receipt = {
        "kind": claim.kind,
        "target": target or claim.kind,
        "label": _ACT_LABELS[claim.kind],
        "detail": detail,
    }
    url = target if _URL_IN_RE.fullmatch(target or "") else ""
    if not url and act is not None and claim.kind in (c.PR, c.ISSUE, c.PUBLISH):
        found = _URL_IN_RE.search(act.text.split("\n", 1)[-1])
        url = found.group(0).rstrip(".,;:") if found else ""
    if url:
        receipt["href"] = url
    return receipt


# ── checks ────────────────────────────────────────────────────────────────


def _fresh(st: os.stat_result, started_at: float | None) -> bool:
    # The modification time only: the change time moves on any metadata
    # update (a tag, an extended attribute), which is not a write. A copy that
    # kept its old times is found by the call that made it instead.
    return started_at is None or st.st_mtime >= started_at - _FRESH_SLACK_S


def _check_folder(claim: c.Claim, ev: Evidence, path: str, shown: str, earlier: bool) -> Verdict:
    try:
        with os.scandir(path) as it:
            entries = list(itertools.islice(it, _MAX_ENTRIES))
    except OSError:
        return Verdict(False, note=f"{shown} cannot be read")
    if not entries:
        return Verdict(False, note=f"{shown} is empty")
    if not claim.made or earlier or ev.started_at is None:
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
        if ev.wrote(path, under=True):
            return Verdict(True, receipt=_path_receipt(c.FOLDER, path, "written in this turn"))
        return Verdict(False, note=f"nothing in {shown} was written in this turn")
    fresh.sort(reverse=True)
    newest = fresh[0][1]
    more = f" and {len(fresh) - 1} more" if len(fresh) > 1 else ""
    return Verdict(True, receipt=_path_receipt(c.FOLDER, path, f"{newest}{more}", fresh[0][0]))


def _check_path(claim: c.Claim, ev: Evidence) -> Verdict:
    target = claim.target
    shown = f"`{target}`"
    if not os.path.isabs(os.path.expanduser(target)) and not ev.workspace:
        # A relative path with no workspace to read it against: a path in
        # prose is let through; the app's own link to a file is not.
        if not claim.deliverable:
            return Verdict(True, checked=False)
        return Verdict(False, note=f"{shown} is in no folder this chat can read")
    path = ev.path(target)
    outright = claim.firm and claim.made and not claim.deliverable
    if ev.plan_targets is not None and path not in ev.plan_targets and not outright:
        return Verdict(True, checked=False)
    earlier = claim.earlier or (path not in ev.tried_paths and ev.said_before_path(path))
    if claim.kind == c.REMOVED:
        if os.path.lexists(path):
            return Verdict(False, note=f"{shown} still exists")
        if earlier or ev.removed(path):
            return Verdict(True, receipt=_path_receipt(c.REMOVED, path, "removed"))
        return Verdict(False, note=f"no call in this turn removed {shown}")
    try:
        st = os.stat(path)
    except OSError:
        return Verdict(False, note=f"{shown} does not exist")
    if stat.S_ISDIR(st.st_mode):
        return _check_folder(claim, ev, path, shown, earlier)
    if st.st_size == 0:
        return Verdict(False, note=f"{shown} is empty")
    if claim.made and not earlier and not _fresh(st, ev.started_at) and not ev.wrote(path):
        return Verdict(False, note=f"{shown} was not written in this turn")
    clock, _iso = _when(st.st_mtime)
    return Verdict(
        True,
        receipt=_path_receipt(c.FILE, path, f"{_size(st.st_size)}, saved {clock}", st.st_mtime),
    )


def _check_act(claim: c.Claim, ev: Evidence) -> Verdict:
    if claim.kind == c.LINK:
        if ev.link_made(claim.target):
            return Verdict(True, receipt=_act_receipt(claim, None))
        if claim.earlier or _url_in(claim.target, ev.earlier_text):
            return Verdict(True, checked=False)
        return Verdict(False, note=_ACT_NOTES[c.LINK].format(target=f"`{claim.target}`"))
    done = [
        a for a in ev.done if a.kind == claim.kind and _target_matches(claim.kind, claim.target, a)
    ]
    if done:
        return Verdict(True, receipt=_act_receipt(claim, done[-1]))
    if claim.earlier or (claim.kind not in ev.tried and ev.said_before_act(claim)):
        return Verdict(True, checked=False)
    to = f" to `{claim.target}`" if claim.target else ""
    return Verdict(False, note=_ACT_NOTES[claim.kind].format(to=to, target=claim.target))


def _tried(claim: c.Claim, ev: Evidence) -> bool:
    """True when the turn tried what the claim says was done: wrote or
    removed its file (or something in its folder), or tried its act."""
    if claim.kind in (c.FILE, c.FOLDER, c.REMOVED):
        if not os.path.isabs(os.path.expanduser(claim.target)) and not ev.workspace:
            return False
        path = ev.path(claim.target).rstrip("/")
        return any(p == path or p.startswith(path + "/") for p in ev.tried_paths)
    return claim.kind in ev.tried


def check(claim: c.Claim, ev: Evidence) -> Verdict:
    """Whether ``claim`` holds, by the rules in the module notes."""
    if not claim.firm and not _tried(claim, ev):
        return Verdict(True, checked=False)
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
