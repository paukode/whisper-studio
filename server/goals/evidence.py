"""Whether what a reply claims actually happened.

A claim (server/goals/claims.py) is checked against the world, never against
the model's word:

- a file must be on disk and not empty, and where the reply says it was
  made, written since the turn began or by a successful call of the turn
  that named that exact path; a folder must hold something, new this turn
  where the reply says it wrote there; a removed file must be gone, and
  removed by a call of the turn (one taken out of git only must be untracked
  by one); a file the reply only says is somewhere must be there, and must
  be new when the turn tried to write it and failed; an artifact card must
  have been made this turn or be one the session holds;
- every other act (a push, a commit, a pull request, a merge, an issue, an
  upload, a message, a deploy, a schedule, a memory, a setting) must be one
  that a successful call of the turn did (server/goals/acts.py reads what each
  call did, server/goals/call_results.py whether it succeeded), or that an
  agent this turn reported, naming the claim's target where the claim names
  one and going where the claim says (an email is not a chat post or a
  message to an agent; a preview is not production); a link must come from a
  call that did something. A call that failed, was refused or denied, still
  waits for approval, or still runs did nothing.

A claim the turn did not back is let through as a recap of earlier work only
when the turn did not try it, the conversation shows it was done (a
delivery an earlier reply verified, which the chat sends along with its
history, or an agent's report), and the claim reads as a recap: it says so
("earlier", "as I mentioned"), it states what now is ("the branch is pushed",
"the report is at X"), or the user asked about earlier work ("so what
happened?", "did you push it?") and not for new work. So "Pushed the retry
fix to main" after an earlier push to main, when the user asked for a new
fix and a push, is held. A claim the reader cannot tell from
a description (a row of a table of changes, what a log says: ``Claim.firm``
False) is checked only when the turn tried that act or wrote that file. In
plan mode a path is checked where a writing call of the turn named it, or
where the reply says it made the file.

Pure functions over the provider-neutral message list, plus stat.
"""

from __future__ import annotations

import itertools
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from urllib.parse import quote

from server.goals import claims as c
from server.goals.acts import Act, reads_only
from server.goals.call_results import Call, merge_calls, report_texts, succeeded, turn_calls
from server.goals.deliverables import (
    last_user_prompt,
    resolve_path,
    targeted_paths,
    turn_messages,
)

__all__ = ["Call", "Evidence", "Verdict", "check", "not_done", "succeeded", "turn_calls"]

# Filesystem times are coarse and a tool may stamp a file a moment before the
# turn's clock started.
_FRESH_SLACK_S = 2.0
# A folder the reply says it wrote to is read this far.
_MAX_ENTRIES = 5000
_SCRIPT_TOOLS = frozenset({"run_python", "run_tool_script"})
_COMMAND_TOOLS = frozenset({"ws_run_command", "terminal_run", "terminal_send", "aws_cli"})
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
# What a call still running was doing, for its note ("Not confirmed yet: the
# deploy is still running.").
_PENDING_WHAT = {
    c.PUSH: "the push",
    c.COMMIT: "the commit",
    c.PR: "the pull request",
    c.MERGE: "the merge",
    c.ISSUE: "the issue",
    c.UPLOAD: "the upload",
    c.MESSAGE: "the message",
    c.PUBLISH: "the deploy",
    c.SCHEDULE: "the schedule",
    c.MEMORY: "the memory write",
    c.SETTING: "the setting change",
}
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*://(?:www\.)?", re.IGNORECASE)
_URL_IN_RE = re.compile(r"https?://[^\s\"'<>()\[\]`\\]+")
_HEX_RUN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_VERSION_ONLY_RE = re.compile(r"v?\d+(?:\.\d+)+")
_NUMBER_TARGET_RE = re.compile(r"#(\d+)")
_SLACK_ID_RE = re.compile(r"^[CDG][A-Z0-9]{8,}$")

# Where a claim says its act went, read from its clause (see _scope_ok).
_CHANNELS = (
    ("email", re.compile(r"\be-?mail(?:ed|s)?\b|\binbox\b|\bmailed\b", re.I)),
    ("comment", re.compile(r"\bcomment(?:ed|s)?\b|\breview(?:ed)?\b|\bapproved\b", re.I)),
    ("sms", re.compile(r"\bsms\b|\btexted\b|\btext message\b", re.I)),
    ("agent", re.compile(r"\b(?:sub-?)?agents?\b", re.I)),
    (
        "chat",
        re.compile(
            r"\b(?:slack|teams|discord|telegram|whatsapp|chat|dm(?:'?e?d)?|channel)\b"
            r"|(?<![\w&])#[a-z][\w-]*",
            re.I,
        ),
    ),
)
_ENVS = (
    ("production", re.compile(r"\b(?:prod|production|live)\b", re.I)),
    ("staging", re.compile(r"\bstag(?:e|ing)\b", re.I)),
    ("preview", re.compile(r"\bpreview\b", re.I)),
)
_FAMILIES = (
    ("testpypi", re.compile(r"\btest\s?pypi\b", re.I)),
    ("pypi", re.compile(r"\bpypi\b", re.I)),
    ("npm", re.compile(r"\bnpm\b", re.I)),
    ("crates", re.compile(r"\bcrates(?:\.io)?\b", re.I)),
    ("rubygems", re.compile(r"\brubygems\b", re.I)),
    ("vercel", re.compile(r"\bvercel\b", re.I)),
    ("netlify", re.compile(r"\bnetlify\b", re.I)),
    ("cloudflare", re.compile(r"\bcloudflare\b|\bworkers?\.dev\b", re.I)),
    ("fly", re.compile(r"\bfly(?:\.io)?\b", re.I)),
    ("firebase", re.compile(r"\bfirebase\b", re.I)),
    ("slack", re.compile(r"\bslack\b", re.I)),
    ("discord", re.compile(r"\bdiscord\b", re.I)),
    ("teams", re.compile(r"\bms\s+teams\b|\bmicrosoft\s+teams\b|\bin\s+teams\b", re.I)),
    ("gitlab", re.compile(r"\bgitlab\b", re.I)),
)
_WHAT = (
    ("image", re.compile(r"\b(?:image|container)s?\b|\becr\b|\bdocker\s?hub\b|\bghcr\b", re.I)),
    (
        "package",
        re.compile(r"\b(?:package|library|crate|gem|wheel|sdk)s?\b|\bnpm\b|\bpypi\b", re.I),
    ),
    (
        "site",
        re.compile(
            r"\b(?:site|website|app|api|service|worker|function|stack|frontend|backend|lambda)s?\b",
            re.I,
        ),
    ),
)
# A turn that asks about earlier work ("so what happened?", "did you push
# it?") rather than for new work: a claim there may rest on earlier work
# without saying so. "Can you push it?" and "go" ask for new work.
_ASKS_ABOUT_RE = re.compile(
    r"^[\s\W]*(?:so\s+|and\s+|ok(?:ay)?\s+|hey\s+|also\s+)?(?:what|which|where|when|who|why|how"
    r"|did|do|does|have|has|is|are|was|were|any\s+(?:update|news)|status|summar\w*|recap"
    r"|remind\s+me|tell\s+me|list|show\s+me|give\s+me\s+(?:a|the)\s+(?:summary|recap|status"
    r"|rundown))\b",
    re.IGNORECASE,
)
_ASKS_FOR_RE = re.compile(
    r"\b(?:can|could|would|will)\s+you\s+(?!(?:tell|remind|summar\w*|list|show|recap|explain"
    r"|check|confirm|see)\b)\w+|\bplease\s+(?!(?:tell|remind|summar\w*|list|show|recap|explain"
    r"|check|confirm)\b)\w+|(?:^|[.!?]\s+|\n)\s*(?:now\s+|then\s+|also\s+|and\s+)?(?:push"
    r"|commit|merge|deploy|publish|release|send|email|post|upload|save|write|create|make|fix"
    r"|update|delete|remove|open|file|schedule|set|add|run|build|ship|draft|generate|export"
    r"|rename|move|copy)\b",
    re.IGNORECASE,
)
# A claim that a file was taken out of git, not off the disk.
_UNTRACKED_RE = re.compile(
    r"\bfrom\s+(?:git|the\s+(?:repo(?:sitory)?|index)|version\s+control|tracking)\b"
    r"|\buntrack(?:ed)?\b|\bstopped\s+tracking\b|\bno\s+longer\s+tracked\b",
    re.I,
)
# A later sentence that corrects a dropped claim names its target and says
# it was not so.
_CORRECTION_RE = re.compile(
    r"\b(?:not|never|no\s+longer|failed|unable|missing|instead|actually|sorry|apolog\w*"
    r"|correction|mistake|wrong|incorrect|cannot)\b|n['\N{RIGHT SINGLE QUOTATION MARK}]t\b",
    re.I,
)
_SENTENCES_RE = re.compile(r"(?<=[.!?])\s+|\n")


@dataclass(frozen=True)
class Verdict:
    """What checking one claim found. ``checked`` False means it could not
    be checked here and is let through (a planned file, a description).
    ``note`` says what is true instead, for the user and the model;
    ``pending`` marks a claim whose act a call started and has not finished;
    ``receipt`` describes what was verified, for the chat's chips."""

    ok: bool
    checked: bool = True
    note: str = ""
    receipt: dict | None = None
    pending: bool = False


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


def names_target(target: str, text: str) -> bool:
    """True when ``text`` names a claim's target as a whole word, or the
    last part of a path of four or more characters."""
    target = target.strip().strip("`")
    if not target:
        return False
    name = target.rstrip("/").rsplit("/", 1)[-1]
    return word_in(target, text) or (len(name) >= 4 and word_in(name, text))


def mentions_as_correction(target: str, text: str) -> bool:
    """True when a sentence of ``text`` names ``target`` and says it was not
    so ("I could not save `q3.docx`"): an instruction that names it ("Open
    `q3.docx` in Keynote") corrects nothing."""
    return any(
        names_target(target, s) and _CORRECTION_RE.search(s) for s in _SENTENCES_RE.split(text)
    )


def _scope_words(text: str) -> set[str]:
    words: set[str] = set()
    for table in (_CHANNELS, _ENVS, _FAMILIES, _WHAT):
        for word, pattern in table:
            if pattern.search(text):
                words.add(word)
                if table is not _FAMILIES:
                    break
    return words


def claim_scope(claim: c.Claim) -> set[str]:
    """Where a claim says its act went (see _scope_ok), read from its
    clause."""
    return _scope_words(claim.clause or claim.target)


_CHANNEL_WORDS = frozenset(w for w, _p in _CHANNELS)
_ENV_WORDS = frozenset(w for w, _p in _ENVS)
_FAMILY_WORDS = frozenset(w for w, _p in _FAMILIES) | frozenset(
    {"github", "aws", "gcp", "azure", "serverless", "ecr", "s3", "git"}
)
_WHAT_WORDS = frozenset({"image", "package", "site", "release", "git"})


def _scope_ok(claim: c.Claim, act: Act) -> bool:
    """True when the act went where the claim says: the same channel for a
    message (a message to an agent or a pull request comment only where the
    claim says so), no other environment for a deploy (a preview is not
    production), the registry or host the claim names, and the same kind of
    thing (a package is not a site, an image push is not a git push)."""
    want = claim_scope(claim)
    have = set(act.scope.split())
    if claim.kind == c.MESSAGE:
        chan = want & _CHANNEL_WORDS
        got = have & _CHANNEL_WORDS
        if got & {"agent", "comment"} and not (chan & got or claim.target.startswith("#")):
            return False
        if chan and got and not chan & got:
            return False
    if claim.kind == c.PUSH:
        image = "image" in want
        if image != ("image" in have) and have & {"image", "git"}:
            return False
    wanted_env, got_env = want & _ENV_WORDS, have & _ENV_WORDS
    if wanted_env and got_env and not wanted_env & got_env:
        return False
    fam = want & (_FAMILY_WORDS - {"git"})
    got_fam = have & _FAMILY_WORDS
    if fam and got_fam and not fam & got_fam:
        return False
    if claim.kind == c.PUBLISH:
        what, got_what = want & {"package", "site"}, have & {"package", "site"}
        if what and got_what and not what & got_what:
            return False
    return True


@dataclass
class Before:
    """What the conversation shows was done before this turn, for a recap:
    the deliveries earlier replies verified (the chat sends them along with
    its history) and what agents reported in rows before this turn."""

    acts: list[Act] = field(default_factory=list)


def _receipt_act(r: dict) -> Act | None:
    kind = str(r.get("kind") or "")
    if not kind:
        return None
    target = str(r.get("target") or "")
    if target == kind:
        target = ""
    text = " ".join(str(r.get(k) or "") for k in ("target", "detail", "href"))
    return Act(kind, target, text)


def _report_acts(texts: list[str], cache: dict | None) -> list[Act]:
    acts: list[Act] = []
    for text in texts:
        key = f"report:{hash(text)}"
        found = cache.get(key) if cache is not None else None
        if found is None:
            found = [
                Act(cl.kind, cl.target, cl.clause, " ".join(sorted(claim_scope(cl))))
                for cl in c.read_claims(text)
                if cl.firm
            ]
            if cache is not None:
                cache[key] = found
        acts += found
    return acts


def before_of(messages: list, receipts: list | None = None, cache: dict | None = None) -> Before:
    """The earlier work a turn's recaps may rest on (see ``Before``)."""
    turn = turn_messages(messages)
    earlier = messages[: len(messages) - len(turn)]
    acts = [a for r in receipts or [] if isinstance(r, dict) and (a := _receipt_act(r))]
    acts += _report_acts(report_texts([], earlier), cache)
    return Before(acts=acts)


@dataclass
class Evidence:
    """What a turn did, as the checks need it."""

    workspace: str | None = None
    started_at: float | None = None
    calls: list[Call] = field(default_factory=list)
    reported: list[Act] = field(default_factory=list)
    before: Before = field(default_factory=Before)
    prompt: str = ""
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
        kept: dict[str, Call] | None = None,
        before: Before | None = None,
        receipts: list | None = None,
        cache: dict | None = None,
    ) -> Evidence:
        """The evidence of the turn ``messages`` ends on. ``kept`` holds
        the turn's calls the stream guard has seen, each as first read, and
        takes those read now; ``extra_calls`` are more calls of the turn the
        messages no longer hold (compaction, a salvage round). ``before``
        (or ``receipts``, the deliveries earlier replies verified) is the
        earlier work a recap may rest on."""
        turn = turn_messages(messages)
        ledger: dict[str, Call] = kept if kept is not None else {}
        for x in extra_calls or []:
            ledger.setdefault(x.id, x)
        merge_calls(ledger, turn_calls(turn, cache))
        calls = list(ledger.values())
        return cls(
            workspace=workspace,
            started_at=started_at,
            calls=calls,
            reported=_report_acts(report_texts(calls, turn), cache),
            before=before if before is not None else before_of(messages, receipts, cache),
            prompt=last_user_prompt(messages),
            has_artifact=session_has_artifact
            or any(x.ok and a.kind == c.ARTIFACT for x in calls for a in x.acts),
            plan_targets=targeted_paths(messages, workspace) if plan_mode else None,
        )

    # ── what the turn did and tried ──────────────────────────────────────

    def path(self, target: str) -> str:
        return resolve_path(target, self.workspace)

    @cached_property
    def done(self) -> list[Act]:
        return [a for x in self.calls if x.ok for a in x.acts] + self.reported

    @cached_property
    def written(self) -> list[str]:
        return [self.path(p) for x in self.calls if x.ok for p in x.writes]

    @cached_property
    def tried_paths(self) -> set[str]:
        out = {self.path(p) for x in self.calls for p in x.writes}
        out |= {self.path(a.target) for x in self.calls for a in x.acts if a.kind == c.REMOVED}
        return out

    def wrote(self, path: str, *, under: bool = False) -> bool:
        """True when a successful call of the turn wrote ``path`` (or, with
        ``under``, something inside it)."""
        folder = path.rstrip("/") + "/"
        return any(p == path or (under and p.startswith(folder)) for p in self.written)

    def removed(self, path: str, *, index: bool = False) -> bool:
        """True when a successful call of the turn removed ``path`` (with
        ``index``, took it out of git)."""
        for x in self.calls:
            if not x.ok:
                continue
            for a in x.acts:
                if a.kind != c.REMOVED or not a.target or (index and a.scope != "index"):
                    continue
                gone = self.path(a.target).rstrip("/")
                if path == gone or path.startswith(gone + "/"):
                    return True
        return False

    def link_made(self, url: str) -> bool:
        """True when a successful call that did more than read names this
        address."""
        return any(
            x.ok and not reads_only(x.name) and _url_in(url, f"{x.text}\n{x.result}")
            for x in self.calls
        ) or any(_url_in(url, a.text) for a in self.reported)

    def seen(self, target: str) -> bool:
        """True when a call of the turn returned this address or pull
        request number: it exists."""
        number = _NUMBER_TARGET_RE.fullmatch(target.strip())
        for x in self.calls:
            if number:
                if re.search(rf"/(?:pull|issues|merge_requests)/{number.group(1)}\b", x.result):
                    return True
            elif _url_in(target, x.result):
                return True
        return False

    def recap(self, claim: c.Claim) -> bool:
        """True when the claim may rest on earlier work: it says it is
        earlier, says what now is, or the user asked about earlier work
        rather than for new work (see the module notes)."""
        if claim.earlier or not claim.made:
            return True
        return bool(_ASKS_ABOUT_RE.search(self.prompt)) and not _ASKS_FOR_RE.search(self.prompt)


# ── matching a claim's target ─────────────────────────────────────────────


def _same_target(a: str, b: str) -> bool:
    def norm(t: str) -> str:
        t = _flat(t).strip().strip("`'\"").lstrip("#@").rstrip("/")
        return t.removeprefix("origin/")

    return norm(a) == norm(b)


# A registry a claim names ("Pushed the image to ECR"), and how the image's
# own name says it went there.
_REGISTRY_HOSTS = (
    (re.compile(r"\becr\b", re.I), re.compile(r"\.ecr\.")),
    (re.compile(r"\bghcr\b", re.I), re.compile(r"^ghcr\.io/")),
    (
        re.compile(r"\bdocker\s?hub\b|\bdocker\.io\b", re.I),
        re.compile(r"^(?:docker\.io/|[^./]+/[^./]+$|[^./]+$)"),
    ),
)


def _target_matches(claim: c.Claim, act: Act) -> bool:
    """True when the act did what the claim names (see the module notes)."""
    kind, want = claim.kind, claim.target.strip()
    if not want:
        return True
    have = act.target.strip()
    text = act.text
    if kind == c.PUSH:
        if "image" in act.scope.split():
            said = claim.clause or want
            for named, host in _REGISTRY_HOSTS:
                if named.search(said):
                    return bool(host.search(have.split(":", 1)[0]))
            return want.lower() in have.lower() or want.lower() == "registry"
        branch = want.rsplit("/", 1)[-1]
        if have and have not in ("HEAD", "tags"):
            return have.rsplit("/", 1)[-1].lower() == branch.lower()
        return word_in(branch, text)
    number = _NUMBER_TARGET_RE.fullmatch(want)
    if number:
        n = number.group(1)
        if have.startswith("#"):
            return have[1:] == n
        if have:
            return bool(re.search(rf"/(?:pull|pulls|issues|merge_requests)/{n}\b", have))
        return bool(
            re.search(rf"/(?:pull|pulls|issues|merge_requests)/{n}\b|\"number\"\s*:\s*{n}\b", text)
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
        if want.startswith("#") and (
            _SLACK_ID_RE.match(have) or (not have and "chat" in act.scope.split())
        ):
            # A channel the call names by id alone, or a webhook's own.
            return True
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


def _kinds_for(claim: c.Claim) -> tuple[str, ...]:
    """The act kinds that meet a claim: its own, a pull request or an issue
    for a bare number ("Opened #7"), and a push or a merge for work that is
    now on a branch."""
    if claim.kind in (c.PR, c.ISSUE) and _NUMBER_TARGET_RE.fullmatch(claim.target.strip()):
        return (c.PR, c.ISSUE)
    if claim.kind == c.PUSH and not claim.made:
        return (c.PUSH, c.MERGE)
    return (claim.kind,)


def _meets(claim: c.Claim, act: Act) -> bool:
    return (
        act.kind in _kinds_for(claim)
        and (act.kind == c.MERGE and claim.kind == c.PUSH or _target_matches(claim, act))
        and _scope_ok(claim, act)
    )


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


def _path_tried(claim: c.Claim, ev: Evidence) -> bool:
    if not os.path.isabs(os.path.expanduser(claim.target)) and not ev.workspace:
        return False
    path = ev.path(claim.target).rstrip("/")
    return any(p == path or p.startswith(path + "/") for p in ev.tried_paths)


def _check_folder(claim: c.Claim, ev: Evidence, path: str, shown: str, tried: bool) -> Verdict:
    try:
        with os.scandir(path) as it:
            entries = list(itertools.islice(it, _MAX_ENTRIES))
    except OSError:
        return Verdict(False, note=f"{shown} cannot be read")
    if not entries:
        return Verdict(False, note=f"{shown} is empty")
    if not claim.made or ev.started_at is None:
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
        if not tried and ev.recap(claim):
            return Verdict(True, receipt=_path_receipt(c.FOLDER, path, f"{len(entries)} items"))
        return Verdict(False, note=f"nothing in {shown} was written in this turn")
    fresh.sort(reverse=True)
    newest = fresh[0][1]
    more = f" and {len(fresh) - 1} more" if len(fresh) > 1 else ""
    return Verdict(True, receipt=_path_receipt(c.FOLDER, path, f"{newest}{more}", fresh[0][0]))


def _check_removed(claim: c.Claim, ev: Evidence, path: str, shown: str, tried: bool) -> Verdict:
    if _UNTRACKED_RE.search(claim.clause):
        if ev.removed(path, index=True) or (not os.path.lexists(path) and ev.removed(path)):
            return Verdict(True, receipt=_path_receipt(c.REMOVED, path, "untracked"))
        if not tried and ev.recap(claim):
            return Verdict(True, checked=False)
        return Verdict(False, note=f"no call in this turn took {shown} out of git")
    if os.path.lexists(path):
        return Verdict(False, note=f"{shown} still exists")
    if ev.removed(path) or (not tried and ev.recap(claim)):
        return Verdict(True, receipt=_path_receipt(c.REMOVED, path, "removed"))
    return Verdict(False, note=f"no call in this turn removed {shown}")


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
    if ev.plan_targets is not None and path not in ev.plan_targets:
        if not (claim.firm and claim.made):
            return Verdict(True, checked=False)
    tried = _path_tried(claim, ev)
    if claim.kind == c.REMOVED:
        return _check_removed(claim, ev, path, shown, tried)
    try:
        st = os.stat(path)
    except OSError:
        return Verdict(False, note=f"{shown} does not exist")
    if stat.S_ISDIR(st.st_mode):
        return _check_folder(claim, ev, path, shown, tried)
    if st.st_size == 0:
        return Verdict(False, note=f"{shown} is empty")
    new = _fresh(st, ev.started_at) or ev.wrote(path)
    if not new and (tried or (claim.made and not ev.recap(claim))):
        # Made, or tried and failed: an older copy on disk is not this turn's.
        return Verdict(False, note=f"{shown} was not written in this turn")
    clock, _iso = _when(st.st_mtime)
    return Verdict(
        True,
        receipt=_path_receipt(c.FILE, path, f"{_size(st.st_size)}, saved {clock}", st.st_mtime),
    )


# A command that runs a program of its own ("python gen_report.py", "bash
# notify.sh"): what it does cannot be read from its words.
_RUNS_CODE_RE = re.compile(
    r"(?:^|[\s;&|(\"])(?:python3?|node|deno|bun|ruby|perl|php|bash|sh|zsh|osascript|\./[\w./-]+)"
    r"(?=\s|$|\")"
)
_BUILDS_RE = re.compile(r"(?:^|[\s;&|(\"])(?:make|npm|pnpm|yarn|cargo|go|npx|uv)(?=\s|$|\")")


def _could(claim: c.Claim, x: Call, ev: Evidence) -> bool:
    """True when call ``x`` does, or may do, what ``claim`` says: it names
    the claim's file or act, writes somewhere it does not name, or runs code
    whose acts cannot be read."""
    if x.name in _SCRIPT_TOOLS:
        return True
    command = x.name in _COMMAND_TOOLS
    if command and _RUNS_CODE_RE.search(x.text):
        return True
    if claim.kind in (c.FILE, c.FOLDER, c.REMOVED):
        if command and _BUILDS_RE.search(x.text):
            return True
        if not command and reads_only(x.name):
            return False
        named = [ev.path(p) for p in x.writes] + [
            ev.path(a.target) for a in x.acts if a.kind == c.REMOVED and a.target
        ]
        if not named:
            return not command
        path = ev.path(claim.target).rstrip("/")
        return any(p == path or p.startswith(path + "/") for p in named)
    if claim.kind == c.ARTIFACT:
        return x.name in ("create_artifact", "edit_artifact")
    return any(_meets(claim, a) for a in x.acts)


def could_make(claim: c.Claim, calls: list[Call], ev: Evidence) -> bool:
    """True when one of ``calls`` (a round's calls, before they run) could
    make ``claim`` true."""
    return any(_could(claim, x, ev) for x in calls)


def _check_act(claim: c.Claim, ev: Evidence) -> Verdict:
    to = f" to `{claim.target}`" if claim.target else ""
    if claim.kind == c.LINK:
        if ev.link_made(claim.target):
            return Verdict(True, receipt=_act_receipt(claim, None))
        if ev.recap(claim) and any(_url_in(claim.target, a.text) for a in ev.before.acts):
            return Verdict(True, checked=False)
        return Verdict(False, note=_ACT_NOTES[c.LINK].format(target=f"`{claim.target}`"))
    done = [a for a in ev.done if _meets(claim, a)]
    if done:
        return Verdict(True, receipt=_act_receipt(claim, done[-1]))
    started = [x for x in ev.calls if x.pending and any(_meets(claim, a) for a in x.acts)]
    if started:
        what = _PENDING_WHAT.get(claim.kind, "it")
        return Verdict(False, note=f"{what}{to} is still running", pending=True)
    tried = any(_meets(claim, a) for x in ev.calls for a in x.acts)
    if not tried:
        if not claim.made and claim.kind in (c.PR, c.ISSUE) and ev.seen(claim.target):
            # "Here's the PR: <address>" where a call of the turn returned it.
            return Verdict(True, receipt=_act_receipt(claim, None))
        if ev.recap(claim) and any(_meets(claim, a) for a in ev.before.acts):
            return Verdict(True, checked=False)
    return Verdict(False, note=_ACT_NOTES[claim.kind].format(to=to, target=claim.target))


def _tried(claim: c.Claim, ev: Evidence) -> bool:
    """True when the turn tried what the claim says was done: wrote or
    removed its file (or something in its folder), or tried its act."""
    if claim.kind in (c.FILE, c.FOLDER, c.REMOVED):
        return _path_tried(claim, ev)
    return any(a.kind in _kinds_for(claim) for x in ev.calls for a in x.acts)


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
    lead = "Not confirmed yet" if verdict.pending else _NOT_DONE[claim.kind]
    return f"{lead}: {verdict.note}."
