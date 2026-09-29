"""What each tool call of a turn did, as the claim check reads it.

A reply's act ("Pushed to main", "Sent the summary to Dana") holds only when
a call of the turn did it, and a call does an act by what it is and what it
was given, never by words that happen to sit in its arguments or its result:
a file written with "git push" in it pushes nothing, and a search result that
lists a pull request opened none. Each call is read for the acts it does:

- the app's own tools by name and input: git_push (a push), git_add_commit,
  git_merge and ws_merge_worktree, git_push_pr (a push, and a pull request
  only when its result says one was created), the github tool by its gh
  argument list, github_api_write by method and endpoint, memory_write,
  config_set, the artifact tools, cron_create and cron_update, send_message
  and send_session_message, ws_delete_file;
- a shell command (ws_run_command, terminal_run, terminal_send, aws_cli) by
  the programs it runs: git push (not a dry run), commit and merge; gh pr,
  issue and release; an aws s3 copy to s3://, put-object, ses and sns; an
  upload tool; a deploy or publish tool; crontab and launchctl; a mail
  program; rm, trash and git rm for removals;
- the code of run_python by the calls that upload, send mail or push;
- an MCP tool by the verb its own name starts with (send_email,
  post_message, create_pull_request, merge_pull_request, create_issue,
  upload_file, deploy, publish). A draft is no message, and a tool whose name
  says it reads (get, list, search...) does nothing.

``writes_of`` names the files a call writes, for the check that a file the
reply says it made was written in this turn.

Pure functions; no I/O.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass

from server.goals import claims as c
from server.security.command_validator import split_subcommands


@dataclass(frozen=True)
class Act:
    """One act a call does: its kind (a claims kind), what it names (a
    branch, a number, an address, a destination, a recipient; "" when it
    names nothing), and the call's arguments and result as text, for a
    claim's target to be looked up in."""

    kind: str
    target: str
    text: str


_COMMAND_KEYS = {
    "ws_run_command": "command",
    "terminal_run": "command",
    "terminal_send": "input",
    "aws_cli": "command",
}
_WRAPPERS = frozenset({"sudo", "env", "time", "nohup", "command", "exec", "caffeinate", "xargs"})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]`\\]+")
_NUMBER_IN_RE = re.compile(r"/(?:pull|pulls|issues|merge_requests)/(\d+)\b")
_SHA_IN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
# Which programs deliver, and the words that make them do it (None: any).
_DEPLOYERS: dict[str, frozenset | None] = {
    "vercel": None,
    "netlify": frozenset({"deploy"}),
    "cdk": frozenset({"deploy"}),
    "sam": frozenset({"deploy"}),
    "serverless": frozenset({"deploy"}),
    "sls": frozenset({"deploy"}),
    "fly": frozenset({"deploy"}),
    "flyctl": frozenset({"deploy"}),
    "firebase": frozenset({"deploy"}),
    "gcloud": frozenset({"deploy"}),
    "kubectl": frozenset({"apply", "rollout", "set"}),
    "terraform": frozenset({"apply"}),
    "helm": frozenset({"install", "upgrade"}),
    "npm": frozenset({"publish"}),
    "yarn": frozenset({"publish"}),
    "pnpm": frozenset({"publish"}),
    "twine": frozenset({"upload"}),
    "cargo": frozenset({"publish"}),
    "poetry": frozenset({"publish"}),
}
_UPLOADERS = {
    "gsutil": frozenset({"cp", "mv", "rsync"}),
    "rclone": frozenset({"copy", "copyto", "sync", "move"}),
}
_MAILERS = frozenset({"mail", "mailx", "sendmail", "mutt", "msmtp"})
_REMOVERS = frozenset({"rm", "trash", "rmdir", "unlink"})
_WRITERS = frozenset({"cp", "mv", "ditto", "rsync", "install", "ln"})
_OUTPUT_FLAGS = frozenset({"-o", "--output", "--out", "--outfile", "--output-file", "-O"})
_READ_WORDS = frozenset(
    {
        "get", "list", "search", "read", "fetch", "find", "query", "view", "describe", "show",
        "status", "check", "inspect", "lookup", "count", "diff", "log", "logs", "blame",
        "preview", "history", "info", "download", "watch", "poll", "receive", "browse",
    }
)  # fmt: skip
_WORD_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _words(name: str) -> list[str]:
    """The words of a tool's own name ("mcp__gmail__sendEmail" -> send,
    email), lower case."""
    own = name.rsplit("__", 1)[-1]
    return [w.lower() for w in _WORD_RE.findall(own.replace("-", "_"))]


def reads_only(name: str) -> bool:
    """True for a tool whose name says it only reads (its first action or
    read word is a read: "ws_read_file", "mcp__jira__getIssue"), or that is
    registered read-only (server/executors) or marked so by its MCP server."""
    words = _words(name)
    first = next((w for w in words if w in _ACTION_WORDS or w in _READ_WORDS), "")
    if first in _READ_WORDS:
        return True
    from server.executors import is_read_only

    if is_read_only(name):
        return True
    if name.startswith("mcp__"):
        from server.mcp_read_only import is_read_only_mcp_tool

        try:
            return is_read_only_mcp_tool(name)
        except Exception:  # noqa: BLE001 - an unreadable registry reads as a write
            return False
    return False


# ── shell commands ─────────────────────────────────────────────────────────


_OPERATORS = frozenset({"|", "|&", "&", ";", "&&", "||"})
_REDIRECTS = frozenset({">", ">>", "<", "<<", ">&", "&>", "2>", "2>>", "<<<"})


def _argvs(command: str) -> list[list[str]]:
    """Each simple command of a shell command line, as its words: split on
    pipes and lists outside quotes, without redirects and their targets, and
    without leading assignments and wrappers ("sudo", "env FOO=1")."""
    out: list[list[str]] = []
    for part in split_subcommands(command or ""):
        try:
            lexer = shlex.shlex(part, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = part.split()
        words: list[str] = []
        skip = False
        for tok in [*tokens, "|"]:
            if skip:
                skip = False
            elif tok in _REDIRECTS:
                skip = True
            elif tok in _OPERATORS:
                while words and (_ASSIGNMENT_RE.match(words[0]) or words[0] in _WRAPPERS):
                    words = words[1:]
                if words:
                    out.append(words)
                words = []
            else:
                words.append(tok)
    return out


def _positionals(words: list[str], takes_value: frozenset = frozenset()) -> list[str]:
    out: list[str] = []
    skip = False
    for w in words:
        if skip:
            skip = False
            continue
        if w.startswith("-"):
            skip = w in takes_value
            continue
        out.append(w)
    return out


def _git_acts(args: list[str], text: str) -> list[Act]:
    rest = _positionals(args, frozenset({"-C", "-c", "--git-dir", "--work-tree"}))
    if not rest:
        return []
    sub, more = rest[0], rest[1:]
    flags = set(args)
    if sub == "push" and not flags & {"--dry-run", "-n"}:
        branch = more[-1].split(":")[-1] if len(more) > 1 else ""
        return [Act(c.PUSH, branch, text)]
    if sub == "commit" and "--dry-run" not in flags:
        return [Act(c.COMMIT, "", text)]
    if sub == "merge" and not flags & {"--abort", "--no-commit"}:
        return [Act(c.MERGE, more[-1] if more else "", text)]
    if sub == "rm":
        return [Act(c.REMOVED, p, text) for p in more]
    return []


def _gh_acts(args: list[str], text: str) -> list[Act]:
    """The acts of a gh command line (its words after "gh")."""
    rest = _positionals(args, frozenset({"-R", "--repo", "-X", "--method", "-H", "--header"}))
    if len(rest) < 2:
        return []
    group, verb, more = rest[0], rest[1], rest[2:]
    number = f"#{more[0]}" if more and more[0].isdigit() else ""
    if group == "pr" and verb == "create":
        return [Act(c.PR, "", text)]
    if group == "pr" and verb == "merge":
        return [Act(c.MERGE, number, text)]
    if group == "issue" and verb == "create":
        return [Act(c.ISSUE, "", text)]
    if group in ("pr", "issue") and verb == "comment":
        return [Act(c.MESSAGE, number, text)]
    if group == "release" and verb == "create":
        return [Act(c.PUBLISH, more[0] if more else "", text)]
    if group == "api":
        method = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-X", "--method")), "")
        fields = any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in args)
        return _api_acts((method or ("POST" if fields else "GET")).upper(), verb, text)
    return []


def _api_acts(method: str, endpoint: str, text: str) -> list[Act]:
    """The acts of a GitHub API call by method and endpoint."""
    path = "/" + endpoint.split("?", 1)[0].strip("/")
    if method == "GET":
        return []
    number = _NUMBER_IN_RE.search(path)
    target = f"#{number.group(1)}" if number else ""
    if method == "PUT" and path.endswith("/merge"):
        return [Act(c.MERGE, target, text)]
    if method == "POST" and re.search(r"/(?:issues|pulls)/\d+/comments$|/comments$", path):
        return [Act(c.MESSAGE, target, text)]
    if method == "POST" and path.endswith("/pulls"):
        return [Act(c.PR, "", text)]
    if method == "POST" and path.endswith("/issues"):
        return [Act(c.ISSUE, "", text)]
    if method == "POST" and path.endswith("/releases"):
        return [Act(c.PUBLISH, "", text)]
    if method in ("PUT", "POST") and "/contents/" in path:
        return [Act(c.COMMIT, "", text)]
    return []


def _aws_acts(args: list[str], text: str) -> list[Act]:
    rest = _positionals(args, frozenset({"--profile", "--region", "--bucket", "--key", "--body"}))
    if len(rest) < 2:
        return []
    service, verb = rest[0], rest[1]
    if service == "s3" and verb in ("cp", "mv", "sync") and len(rest) >= 4:
        dst = rest[-1]
        return [Act(c.UPLOAD, dst, text)] if dst.startswith("s3://") else []
    if service == "s3api" and verb == "put-object":
        bucket = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--bucket"), "")
        key = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--key"), "")
        return [Act(c.UPLOAD, f"s3://{bucket}/{key}" if bucket else "", text)]
    if (service, verb) in (("ses", "send-email"), ("sesv2", "send-email"), ("sns", "publish")):
        return [Act(c.MESSAGE, "", text)]
    if (service, verb) in (("cloudformation", "deploy"), ("lambda", "update-function-code")):
        return [Act(c.PUBLISH, "", text)]
    return []


def _command_acts(command: str, text: str) -> list[Act]:
    """The acts a shell command line does, program by program."""
    acts: list[Act] = []
    for words in _argvs(command):
        prog, args = os.path.basename(words[0]), words[1:]
        if prog == "git":
            acts += _git_acts(args, text)
        elif prog == "gh":
            acts += _gh_acts(args, text)
        elif prog == "aws":
            acts += _aws_acts(args, text)
        elif prog == "docker" and args[:1] == ["push"]:
            acts.append(Act(c.PUSH, args[-1] if len(args) > 1 else "", text))
        elif prog in _DEPLOYERS:
            wanted = _DEPLOYERS[prog]
            if wanted is None or wanted & set(args):
                acts.append(Act(c.PUBLISH, "", text))
        elif prog in _UPLOADERS and _UPLOADERS[prog] & set(args[:1]):
            acts.append(Act(c.UPLOAD, _positionals(args)[-1] if _positionals(args) else "", text))
        elif prog in ("scp", "rsync") and _positionals(args) and ":" in _positionals(args)[-1]:
            acts.append(Act(c.UPLOAD, _positionals(args)[-1], text))
        elif prog == "crontab" and args and not set(args) & {"-l", "-r"}:
            acts.append(Act(c.SCHEDULE, "", text))
        elif prog == "launchctl" and args[:1] in (["load"], ["bootstrap"]):
            acts.append(Act(c.SCHEDULE, "", text))
        elif prog in _MAILERS:
            acts.append(Act(c.MESSAGE, "", text))
        elif prog in _REMOVERS:
            acts += [Act(c.REMOVED, p, text) for p in _positionals(args)]
    return acts


_CODE_ACTS = (
    (c.UPLOAD, re.compile(r"\.(?:upload_file|upload_fileobj|put_object)\s*\(")),
    (c.MESSAGE, re.compile(r"\bsmtplib\b|\.send_message\s*\(|\.sendmail\s*\(")),
    (c.PUSH, re.compile(r"""["']git["']\s*,\s*["']push["']|["']git push\b""")),
)


_ACTION_WORDS = frozenset(
    {
        "send", "post", "reply", "respond", "forward", "dm", "notify", "chat", "create", "add",
        "write", "open", "file", "merge", "upload", "put", "push", "commit", "update", "deploy",
        "publish", "release", "schedule", "edit", "delete", "remove", "save", "set", "run",
    }
)  # fmt: skip


def _mcp_acts(name: str, text: str) -> list[Act]:
    # The verb is the first action or read word, past a product prefix
    # ("slack_post_message", "github_create_issue").
    words = _words(name)
    at = next((i for i, w in enumerate(words) if w in _ACTION_WORDS or w in _READ_WORDS), None)
    if at is None or words[at] in _READ_WORDS:
        # Creating or saving a draft does nothing the nouns below name;
        # sending one (send_draft) is a message.
        return []
    verb, nouns = words[at], set(words[at + 1 :])
    if verb in ("send", "post", "reply", "respond", "forward", "dm", "notify", "chat") or (
        verb in ("create", "add", "write") and nouns & {"message", "comment", "reply", "post"}
    ):
        return [Act(c.MESSAGE, "", text)]
    if verb == "merge":
        return [Act(c.MERGE, "", text)]
    if verb in ("create", "open") and nouns & {"pull", "pr", "merge"}:
        return [Act(c.PR, "", text)]
    if verb in ("create", "open", "file") and nouns & {"issue", "ticket", "bug"}:
        return [Act(c.ISSUE, "", text)]
    if verb in ("upload", "put"):
        return [Act(c.UPLOAD, "", text)]
    if verb == "push":
        return [Act(c.PUSH, "", text), Act(c.COMMIT, "", text)]
    if verb in ("commit",) or (verb in ("create", "update") and nouns & {"file", "files"}):
        return [Act(c.COMMIT, "", text)]
    if verb in ("deploy", "publish", "release") or (verb == "create" and "release" in nouns):
        return [Act(c.PUBLISH, "", text)]
    if verb == "schedule" or (
        verb in ("create", "add") and nouns & {"event", "reminder", "schedule", "cron", "job"}
    ):
        return [Act(c.SCHEDULE, "", text)]
    return []


def acts_of(name: str, given: dict, text: str, result: str) -> list[Act]:
    """The acts one call does, or tries to (see the module notes). ``text``
    is its arguments as text. Whether it succeeded is the caller's to read: a
    command still running in the background tried its acts and has not done
    them yet."""
    hay = f"{text}\n{result}"
    if name == "git_push":
        return [Act(c.PUSH, str(given.get("branch") or ""), hay)]
    if name == "git_push_pr":
        acts = [Act(c.PUSH, str(given.get("branch") or ""), hay)]
        if "created PR" in result and not re.search(r"PR creation (?:failed|skipped)", result):
            found = _URL_RE.search(result)
            acts.append(Act(c.PR, found.group(0) if found else "", hay))
        return acts
    if name == "git_add_commit":
        sha = _SHA_IN_RE.search(result)
        return [Act(c.COMMIT, sha.group(0) if sha else "", hay)]
    if name in ("git_merge", "ws_merge_worktree"):
        return [Act(c.MERGE, str(given.get("branch") or given.get("name") or ""), hay)]
    if name == "github":
        return _gh_acts([str(a) for a in given.get("args") or []], hay)
    if name == "github_api_write":
        if given.get("graphql_mutation"):
            return []
        return _api_acts(
            str(given.get("method") or "POST").upper(), str(given.get("endpoint") or ""), hay
        )
    if name in ("create_artifact", "edit_artifact"):
        return [Act(c.ARTIFACT, "", hay)]
    if name == "memory_write":
        return [Act(c.MEMORY, "", hay)]
    if name == "config_set":
        return [Act(c.SETTING, "", hay)]
    if name in ("cron_create", "cron_update"):
        return [Act(c.SCHEDULE, str(given.get("name") or ""), hay)]
    if name in ("send_message", "send_session_message"):
        return [
            Act(c.MESSAGE, str(given.get("to_agent_id") or given.get("to_session_id") or ""), hay)
        ]
    if name == "ws_delete_file":
        return [Act(c.REMOVED, str(given.get("path") or ""), hay)]
    if name in _COMMAND_KEYS:
        command = str(given.get(_COMMAND_KEYS[name]) or "")
        if name == "aws_cli" and not command.lstrip().startswith("aws "):
            command = f"aws {command}"
        return _command_acts(command, hay)
    if name == "run_python":
        code = str(given.get("code") or "")
        return [Act(kind, "", hay) for kind, pattern in _CODE_ACTS if pattern.search(code)]
    if name.startswith("mcp__"):
        return _mcp_acts(name, hay)
    return []


# ── files written ──────────────────────────────────────────────────────────

_PATH_KEYS = ("path", "destination_path", "file_path", "filename", "target_path", "output_path")
_REDIRECT_RE = re.compile(r"(?:^|[^<>&0-9])>{1,2}\s*([^\s;&|<>]+)")


def writes_of(name: str, given: dict) -> list[str]:
    """The files one call writes, as it names them: a writing tool's path
    argument, a shell command's redirect targets, output flags and the
    destination of a copy or move. A tool that only reads writes nothing."""
    if name in _COMMAND_KEYS:
        command = str(given.get(_COMMAND_KEYS[name]) or "")
        out = [m.group(1) for m in _REDIRECT_RE.finditer(command)]
        for words in _argvs(command):
            prog, args = os.path.basename(words[0]), words[1:]
            out += [args[i + 1] for i, a in enumerate(args[:-1]) if a in _OUTPUT_FLAGS]
            if prog in _WRITERS and _positionals(args):
                out.append(_positionals(args)[-1])
            if prog in ("touch", "tee", "mkdir"):
                out += _positionals(args)
        return out
    from server.goals.deliverables import OUTSIDE_WRITERS, _write_targets

    if name in OUTSIDE_WRITERS:
        return _write_targets(name, given)
    if reads_only(name):
        return []
    return [str(given[k]) for k in _PATH_KEYS if isinstance(given.get(k), str) and given[k].strip()]
