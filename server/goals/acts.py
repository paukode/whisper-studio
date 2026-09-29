"""What each tool call of a turn did, as the claim check reads it.

A reply's act ("Pushed to main", "Sent the summary to Dana") holds only when
a call of the turn did it, and a call does an act by what it is and what it
was given, never by words that happen to sit in its arguments or its result:
a file written with "git push" in it pushes nothing, and a search result that
lists a pull request opened none. Each call is read for the acts it does:

- the app's own tools by name and input: git_push (a push, unless the remote
  already had everything), git_add_commit, git_merge and ws_merge_worktree,
  git_push_pr (a push, and a pull request only when its result says one was
  created, named by the address in that result), the github tool by its gh
  argument list, github_api_write by method and endpoint (or the mutation it
  runs), memory_write, config_set, the artifact tools, cron_create and
  cron_update, send_message and send_session_message (a message to an
  agent), ws_delete_file;
- a shell command (ws_run_command, terminal_run, terminal_send, aws_cli) by
  the programs it runs, past runners such as npx, "python -m" and "uv run":
  git push, commit, merge, rm and mv; gh, glab and hub; an aws s3 copy to
  s3://, put-object, ses and sns, a deploy; docker push; a deploy or publish
  tool, a make target or package script named for one; an upload tool; curl
  to a chat or mail webhook; crontab and launchctl; a mail program; rm and
  mv for removals. A dry run, a draft release, an auto-merge still waiting
  for its checks and a push that deletes a branch do none of it;
- the code of run_python by the calls that upload, send mail or push;
- an MCP tool by the verb its own name starts with (send_email,
  post_message, create_pull_request, merge_pull_request, create_issue,
  upload_file, deploy, publish), naming what its arguments name (a pull
  request's number, a channel, a recipient). A draft is no message, and a
  tool whose name says it reads (get, list, search...) does nothing.

Each act carries a scope: words for where it went (an email, a chat, a pull
request comment, an agent; a package registry or a site; production, a
preview or staging), so a claim that names one is not met by another.

``writes_of`` names the files a call writes, for the check that a file the
reply says it made was written in this turn.

Pure functions; no I/O but the read-only lookup of MCP tools.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass

from server.goals import claims as c
from server.security.command_validator import split_subcommands, strip_heredoc_bodies


@dataclass(frozen=True)
class Act:
    """One act a call does: its kind (a claims kind), what it names (a
    branch, a number, an address, a destination, a recipient; "" when it
    names nothing), the call's arguments and result as text, for a claim's
    target to be looked up in, and its scope (see the module notes)."""

    kind: str
    target: str
    text: str
    scope: str = ""


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
# Programs that run another one: the program is the first word after them
# that is not a flag ("npx -y vercel deploy", "python -m twine upload").
_RUNNERS = {
    "npx": (),
    "bunx": (),
    "pnpx": (),
    "uvx": (),
    "pnpm": ("dlx", "exec"),
    "yarn": ("dlx", "exec"),
    "npm": ("exec",),
    "bun": ("x",),
    "uv": ("run", "tool run"),
    "pipx": ("run",),
    "poetry": ("run",),
    "pdm": ("run",),
    "hatch": ("run",),
    "bundle": ("exec",),
    "python": ("-m",),
    "python3": ("-m",),
    "py": ("-m",),
}
_DRY_RUN = frozenset({"--dry-run", "--dryrun", "--what-if", "--whatif", "--noop", "--simulate"})
# Which programs deliver, and the words that make them do it (None: any).
_DEPLOYERS: dict[str, frozenset | None] = {
    "vercel": None,
    "netlify": frozenset({"deploy"}),
    "wrangler": frozenset({"deploy", "publish"}),
    "cdk": frozenset({"deploy"}),
    "sam": frozenset({"deploy"}),
    "serverless": frozenset({"deploy"}),
    "sls": frozenset({"deploy"}),
    "fly": frozenset({"deploy"}),
    "flyctl": frozenset({"deploy"}),
    "firebase": frozenset({"deploy", "hosting:channel:deploy"}),
    "gcloud": frozenset({"deploy"}),
    "kubectl": frozenset({"apply", "rollout", "set"}),
    "terraform": frozenset({"apply"}),
    "pulumi": frozenset({"up"}),
    "helm": frozenset({"install", "upgrade"}),
    "railway": frozenset({"up"}),
    "az": frozenset({"deploy", "up"}),
    "heroku": frozenset({"deploy"}),
}
# Package publishers and the registry each publishes to.
_PUBLISHERS = {
    "npm": ("publish", "npm"),
    "yarn": ("publish", "npm"),
    "pnpm": ("publish", "npm"),
    "bun": ("publish", "npm"),
    "twine": ("upload", "pypi"),
    "uv": ("publish", "pypi"),
    "poetry": ("publish", "pypi"),
    "flit": ("publish", "pypi"),
    "hatch": ("publish", "pypi"),
    "cargo": ("publish", "crates"),
    "gem": ("push", "rubygems"),
}
_UPLOADERS = {
    "gsutil": frozenset({"cp", "mv", "rsync"}),
    "rclone": frozenset({"copy", "copyto", "sync", "move", "moveto"}),
}
_MAILERS = frozenset({"mail", "mailx", "sendmail", "mutt", "msmtp"})
_REMOVERS = frozenset({"rm", "trash", "rmdir", "unlink"})
_WRITERS = frozenset({"cp", "mv", "ditto", "rsync", "install", "ln"})
_OUTPUT_FLAGS = frozenset({"-o", "--output", "--out", "--outfile", "--output-file", "-O"})
# A make target or package script named for a delivery ("make deploy", "npm
# run release", "bash scripts/deploy.sh prod").
_DELIVERY_NAME_RE = re.compile(r"(?:^|[-_:/.])(deploy|publish|release|ship)(?:$|[-_:.])")
# Services a command posts a message to, by host (and path).
_MESSAGE_HOSTS = (
    (re.compile(r"hooks\.slack\.com|slack\.com/api/chat\."), "chat slack"),
    (re.compile(r"discord(?:app)?\.com/api/webhooks"), "chat discord"),
    (re.compile(r"api\.telegram\.org/bot"), "chat telegram"),
    (re.compile(r"webhook\.office\.com|outlook\.office\.com/webhook"), "chat teams"),
    (re.compile(r"api\.sendgrid\.com|api\.mailgun\.net|api\.postmarkapp\.com"), "email"),
    (re.compile(r"api\.twilio\.com/.*/Messages"), "sms"),
)
# Words in a call's arguments that say which environment a deploy went to.
_ENV_WORDS = (
    ("production", re.compile(r"(?:^|[\s=:/_-])(?:prod|production)(?:$|[\s=:/_.-])", re.I)),
    ("staging", re.compile(r"(?:^|[\s=:/_-])(?:stage|staging)(?:$|[\s=:/_.-])", re.I)),
    (
        "preview",
        re.compile(r"(?:^|[\s=:/_-])(?:preview|dev|draft|test|testpypi)(?:$|[\s=:/_.-])", re.I),
    ),
)
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


# A tool's read-only mark, looked up at most once a few seconds: the MCP
# registry reads its settings file each time.
_READS_ONLY_TTL_S = 5.0
_reads_only_seen: dict[str, tuple[float, bool]] = {}


def reads_only(name: str) -> bool:
    """True for a tool whose name says it only reads (its first action or
    read word is a read: "ws_read_file", "mcp__jira__getIssue"), or that is
    registered read-only (server/executors) or marked so by its MCP server."""
    now = time.monotonic()
    seen = _reads_only_seen.get(name)
    if seen is not None and now - seen[0] < _READS_ONLY_TTL_S:
        return seen[1]
    answer = _reads_only(name)
    if len(_reads_only_seen) > 512:
        _reads_only_seen.clear()
    _reads_only_seen[name] = (now, answer)
    return answer


def _reads_only(name: str) -> bool:
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
    pipes and lists outside quotes, without heredoc bodies, redirects and
    their targets, leading assignments and wrappers ("sudo", "env FOO=1")."""
    out: list[list[str]] = []
    for part in split_subcommands(strip_heredoc_bodies(command or "")):
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


def _unwrap(words: list[str]) -> list[str]:
    """The command a runner runs ("npx -y vercel --prod" -> vercel --prod),
    or the words as they are."""
    for _ in range(3):
        prog = os.path.basename(words[0]) if words else ""
        subs = _RUNNERS.get(prog)
        if subs is None:
            return words
        rest = words[1:]
        if subs:
            joined = " ".join(rest[:2])
            sub = next((s for s in subs if joined == s or joined.startswith(s + " ")), None)
            if sub is None:
                return words
            rest = rest[len(sub.split()) :]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        if not rest:
            return words
        words = rest
    return words


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


def _dry_run(args: list[str]) -> bool:
    return any(a in _DRY_RUN or a.startswith("--dry-run=") for a in args)


def _env_of(text: str) -> str:
    """The environment a deploy's arguments name, or ""."""
    return next((env for env, pattern in _ENV_WORDS if pattern.search(text)), "")


def _git_acts(args: list[str], text: str) -> list[Act]:
    rest = _positionals(args, frozenset({"-C", "-c", "--git-dir", "--work-tree", "-o"}))
    if not rest:
        return []
    sub, more = rest[0], rest[1:]
    flags = set(args)
    if sub == "push":
        if flags & {"--dry-run", "-n", "--delete", "-d"} or "Everything up-to-date" in text:
            return []
        refspecs = more[1:]
        if any(r.startswith(":") for r in refspecs):
            return []
        branch = refspecs[-1].split(":")[-1] if refspecs else ""
        if "--tags" in flags and not branch:
            branch = "tags"
        acts = [Act(c.PUSH, branch, text, "git")]
        if any("merge_request.create" in a for a in args):
            acts.append(Act(c.PR, "", text, "gitlab"))
        return acts
    if sub == "commit" and "--dry-run" not in flags:
        return [Act(c.COMMIT, "", text)]
    if sub == "merge" and not flags & {"--abort", "--no-commit", "--squash"}:
        if "Already up to date" in text:
            return []
        return [Act(c.MERGE, more[-1] if more else "", text)]
    if sub == "rm" and not flags & {"--dry-run", "-n"}:
        scope = "index" if "--cached" in flags else ""
        return [Act(c.REMOVED, p, text, scope) for p in more]
    if sub == "mv" and len(more) >= 2:
        return [Act(c.REMOVED, p, text, "moved") for p in more[:-1]]
    return []


_GH_VALUES = frozenset(
    {
        "-R", "--repo", "-X", "--method", "-H", "--header", "--head", "-b", "--body", "-t",
        "--title", "-F", "--body-file", "-B", "--base", "-n", "--notes", "--notes-file",
        "--target", "-m", "--milestone", "-l", "--label", "-a", "--assignee", "-r", "--reviewer",
    }
)  # fmt: skip


def _gh_acts(args: list[str], text: str, result: str = "") -> list[Act]:
    """The acts of a gh command line (its words after "gh"). A pull request
    it opens is the one its output names."""
    rest = _positionals(args, _GH_VALUES)
    if len(rest) < 2 or _dry_run(args):
        return []
    group, verb, more = rest[0], rest[1], rest[2:]
    number = f"#{more[0]}" if more and more[0].isdigit() else ""
    flags = set(args)
    if group == "pr" and verb == "create":
        found = _NUMBER_IN_RE.search(result)
        return [Act(c.PR, f"#{found.group(1)}" if found else "", text, "github")]
    if group == "pr" and verb == "merge":
        # An auto-merge waits for the checks; it has merged nothing yet.
        if "--auto" in flags or "will be automatically merged" in text:
            return []
        return [Act(c.MERGE, number, text)]
    if group == "issue" and verb == "create":
        return [Act(c.ISSUE, "", text, "github")]
    if group in ("pr", "issue") and verb in ("comment", "review"):
        return [Act(c.MESSAGE, number, text, "comment github")]
    if group == "release" and verb == "create":
        if "--draft" in flags or "-d" in flags:
            return []
        return [Act(c.PUBLISH, more[0] if more else "", text, "release github")]
    if group == "api":
        method = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-X", "--method")), "")
        fields = any(a in ("-f", "-F", "--field", "--raw-field", "--input") for a in args)
        return _api_acts((method or ("POST" if fields else "GET")).upper(), verb, text)
    return []


def _glab_acts(args: list[str], text: str, prog: str) -> list[Act]:
    """glab (GitLab) and hub, which name a pull request a merge request or
    a "pull-request"."""
    rest = _positionals(args, frozenset({"-R", "--repo", "-m", "--message", "-b", "--base"}))
    if not rest or _dry_run(args):
        return []
    if prog == "hub":
        if rest[0] == "pull-request":
            return [Act(c.PR, "", text, "github")]
        if rest[:2] == ["release", "create"] and "--draft" not in args and "-d" not in args:
            return [Act(c.PUBLISH, rest[2] if len(rest) > 2 else "", text, "release github")]
        return []
    group, verb = rest[0], rest[1] if len(rest) > 1 else ""
    number = f"#{rest[2]}" if len(rest) > 2 and rest[2].isdigit() else ""
    if group == "mr" and verb == "create":
        return [Act(c.PR, "", text, "gitlab")]
    if group == "mr" and verb == "merge":
        return [Act(c.MERGE, number, text)]
    if group == "mr" and verb in ("note", "comment"):
        return [Act(c.MESSAGE, number, text, "comment gitlab")]
    if group == "issue" and verb == "create":
        return [Act(c.ISSUE, "", text, "gitlab")]
    if group == "release" and verb == "create":
        return [Act(c.PUBLISH, rest[2] if len(rest) > 2 else "", text, "release gitlab")]
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
    if method == "POST" and re.search(r"/(?:issues|pulls)/\d+/(?:comments|reviews)$", path):
        return [Act(c.MESSAGE, target, text, "comment github")]
    if method == "POST" and path.endswith("/comments"):
        return [Act(c.MESSAGE, target, text, "comment github")]
    if method == "POST" and path.endswith("/pulls"):
        found = re.search(r'"number"\s*:\s*(\d+)', text)
        return [Act(c.PR, f"#{found.group(1)}" if found else "", text, "github")]
    if method == "POST" and path.endswith("/issues"):
        return [Act(c.ISSUE, "", text, "github")]
    if method == "POST" and path.endswith("/releases"):
        if re.search(r'"draft"\s*:\s*true', text):
            return []
        return [Act(c.PUBLISH, "", text, "release github")]
    if method in ("PUT", "POST") and "/contents/" in path:
        return [Act(c.COMMIT, "", text)]
    return []


_MUTATION_ACTS = (
    (re.compile(r"\bmergePullRequest\b"), c.MERGE, ""),
    (re.compile(r"\b(?:addComment|addPullRequestReview)\b"), c.MESSAGE, "comment github"),
    (re.compile(r"\bcreatePullRequest\b"), c.PR, "github"),
    (re.compile(r"\bcreateIssue\b"), c.ISSUE, "github"),
)


def _aws_acts(args: list[str], text: str) -> list[Act]:
    rest = _positionals(args, frozenset({"--profile", "--region", "--bucket", "--key", "--body"}))
    if len(rest) < 2 or _dry_run(args):
        return []
    service, verb = rest[0], rest[1]
    if service == "s3" and verb in ("cp", "mv", "sync") and len(rest) >= 4:
        dst = rest[-1]
        return [Act(c.UPLOAD, dst, text, "s3")] if dst.startswith("s3://") else []
    if service == "s3api" and verb == "put-object":
        bucket = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--bucket"), "")
        key = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--key"), "")
        return [Act(c.UPLOAD, f"s3://{bucket}/{key}" if bucket else "", text, "s3")]
    if (service, verb) in (("ses", "send-email"), ("sesv2", "send-email")):
        return [Act(c.MESSAGE, "", text, "email")]
    if (service, verb) == ("sns", "publish"):
        return [Act(c.MESSAGE, "", text, "sms" if "--phone-number" in args else "")]
    if (service, verb) in (
        ("cloudformation", "deploy"),
        ("lambda", "update-function-code"),
        ("ecs", "update-service"),
        ("amplify", "start-deployment"),
        ("apprunner", "start-deployment"),
        ("elasticbeanstalk", "update-environment"),
    ):
        return [Act(c.PUBLISH, "", text, f"site aws {_env_of(' '.join(args))}".strip())]
    return []


def _deploy_acts(prog: str, args: list[str], text: str) -> list[Act]:
    """A deploy tool's act, scoped by the environment it went to (vercel and
    netlify deploy a preview unless told --prod)."""
    wanted = _DEPLOYERS[prog]
    words = set(args)
    if _dry_run(args) or (wanted is not None and not wanted & words):
        return []
    joined = " ".join(args)
    if prog == "kubectl":
        rest = _positionals(args, frozenset({"-n", "--namespace", "--context", "-f", "--filename"}))
        does = rest[:1] in (["apply"], ["set"], ["replace"], ["patch"]) or (
            rest[:1] == ["rollout"] and rest[1:2] in (["restart"], ["undo"])
        )
        if not does:
            return []
    if prog == "vercel":
        first = next(iter(_positionals(args)), "")
        if (
            first
            and first not in ("deploy", "promote", "rollback", "redeploy")
            and "/" not in first
        ):
            return []
        target = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--target"), "")
        prod = words & {"--prod", "--production", "--target=production"} or target == "production"
        env = "production" if prod or first in ("promote", "rollback") else "preview"
    elif prog == "netlify":
        env = "production" if "--prod" in words else "preview"
    elif prog == "firebase" and "hosting:channel:deploy" in words:
        env = "preview"
    else:
        env = _env_of(joined)
    family = {"wrangler": "cloudflare", "flyctl": "fly", "sls": "serverless"}.get(prog, prog)
    return [Act(c.PUBLISH, "", text, f"site {family} {env}".strip())]


def _curl_acts(args: list[str], text: str) -> list[Act]:
    """curl, wget or httpie posting to a chat or mail service, or to the
    GitHub API."""
    urls = [a for a in args if a.startswith(("http://", "https://"))]
    if not urls:
        return []
    method = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-X", "--request")), "")
    sends = method.upper() in ("POST", "PUT", "PATCH") or any(
        a in ("-d", "--data", "--data-raw", "--data-binary", "--json", "-F", "--form")
        or a.startswith(("--data=", "--json="))
        for a in args
    )
    if not sends:
        return []
    for url in urls:
        for pattern, scope in _MESSAGE_HOSTS:
            if pattern.search(url):
                return [Act(c.MESSAGE, "", text, scope)]
        found = re.match(r"https?://api\.github\.com(/[^\s?]*)", url)
        if found:
            return _api_acts((method or "POST").upper(), found.group(1), text)
    return []


def _one_command(words: list[str], text: str, result: str) -> list[Act]:
    words = _unwrap(words)
    prog, args = os.path.basename(words[0]), words[1:]
    if prog == "git":
        return _git_acts(args, text)
    if prog == "gh":
        return _gh_acts(args, text, result)
    if prog in ("glab", "hub"):
        return _glab_acts(args, text, prog)
    if prog == "aws":
        return _aws_acts(args, text)
    if prog == "docker" and not _dry_run(args):
        rest = _positionals(args, frozenset({"-t", "--tag", "-f", "--file", "--platform"}))
        if rest[:1] == ["push"] or rest[:2] == ["image", "push"]:
            image = rest[-1] if len(rest) > 1 else ""
            return [Act(c.PUSH, image, text, "image")]
        if "build" in rest[:2] and "--push" in args:
            tag = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-t", "--tag")), "")
            return [Act(c.PUSH, tag, text, "image")]
        return []
    if prog in _PUBLISHERS and _PUBLISHERS[prog][0] in args[:1] and not _dry_run(args):
        joined = " ".join(args)
        registry = "testpypi" if "testpypi" in joined.lower() else _PUBLISHERS[prog][1]
        return [Act(c.PUBLISH, "", text, f"package {registry} {_env_of(joined)}".strip())]
    if prog in _DEPLOYERS:
        return _deploy_acts(prog, args, text)
    if prog in ("make", "gmake") and not (_dry_run(args) or "-n" in args):
        for target in _positionals(args, frozenset({"-C", "-f", "-j"})):
            found = _DELIVERY_NAME_RE.search(target)
            if found:
                what = "package" if found.group(1) == "publish" else "site"
                return [Act(c.PUBLISH, "", text, f"{what} {_env_of(' '.join(args))}".strip())]
        return []
    if prog in ("npm", "yarn", "pnpm", "bun") and not _dry_run(args):
        rest = _positionals(args)
        script = rest[1] if rest[:1] == ["run"] and len(rest) > 1 else rest[0] if rest else ""
        found = _DELIVERY_NAME_RE.search(script)
        if found:
            what = "package npm" if found.group(1) == "publish" else "site"
            return [Act(c.PUBLISH, "", text, f"{what} {_env_of(' '.join(args))}".strip())]
        return []
    if prog in ("bash", "sh", "zsh") or prog.endswith(".sh"):
        script = prog if prog.endswith(".sh") else next(iter(_positionals(args)), "")
        found = _DELIVERY_NAME_RE.search(os.path.basename(script).removesuffix(".sh"))
        if found and not _dry_run(args):
            return [Act(c.PUBLISH, "", text, f"site {_env_of(' '.join(args))}".strip())]
        return []
    if prog in _UPLOADERS and _UPLOADERS[prog] & set(args[:1]) and not _dry_run(args):
        dest = _positionals(args)
        return [Act(c.UPLOAD, dest[-1] if dest else "", text)]
    if prog in ("scp", "rsync") and _positionals(args) and ":" in _positionals(args)[-1]:
        if _dry_run(args) or any(re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", a) for a in args):
            return []
        return [Act(c.UPLOAD, _positionals(args)[-1], text)]
    if prog in ("curl", "wget", "http", "https"):
        return _curl_acts(args, text)
    if prog == "crontab" and args and not set(args) & {"-l", "-r"}:
        return [Act(c.SCHEDULE, "", text)]
    if prog == "launchctl" and args[:1] in (["load"], ["bootstrap"]):
        return [Act(c.SCHEDULE, "", text)]
    if prog in _MAILERS:
        return [Act(c.MESSAGE, "", text, "email")]
    if prog in _REMOVERS:
        return [Act(c.REMOVED, p, text) for p in _positionals(args)]
    if prog == "mv":
        rest = _positionals(args, frozenset({"-t", "--target-directory"}))
        return [Act(c.REMOVED, p, text, "moved") for p in rest[:-1]]
    return []


def _command_acts(command: str, text: str, result: str = "") -> list[Act]:
    """The acts a shell command line does, program by program."""
    return [a for words in _argvs(command) for a in _one_command(words, text, result)]


_CODE_ACTS = (
    (c.UPLOAD, re.compile(r"\.(?:upload_file|upload_fileobj|put_object)\s*\("), "s3"),
    (c.MESSAGE, re.compile(r"\bsmtplib\b|\.send_message\s*\(|\.sendmail\s*\("), "email"),
    (c.PUSH, re.compile(r"""["']git["']\s*,\s*["']push["']|["']git push\b"""), "git"),
)


_ACTION_WORDS = frozenset(
    {
        "send", "post", "reply", "respond", "forward", "dm", "notify", "chat", "create", "add",
        "write", "open", "file", "merge", "upload", "put", "push", "commit", "update", "deploy",
        "publish", "release", "schedule", "edit", "delete", "remove", "save", "set", "run",
        "comment", "approve", "review",
    }
)  # fmt: skip
# Where an MCP tool's messages go, by the words of its server and own name.
_MCP_CHANNELS = (
    (frozenset({"gmail", "outlook", "mail", "email", "smtp", "sendgrid", "mailgun"}), "email"),
    (
        frozenset({"slack", "discord", "teams", "telegram", "whatsapp", "mattermost", "chat"}),
        "chat",
    ),
    (frozenset({"sms", "twilio", "text"}), "sms"),
)
_NUMBER_KEYS = (
    "pullNumber", "pull_number", "prNumber", "pr_number", "pull_request_number",
    "issue_number", "issueNumber", "mergeRequestIid", "merge_request_iid", "iid", "number",
)  # fmt: skip
_CHANNEL_KEYS = ("channel", "channel_id", "channelId", "channel_name", "conversation", "room")
_RECIPIENT_KEYS = ("to", "recipient", "recipients", "email", "emails", "cc", "user", "user_id")


def _given_target(given: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = given.get(key)
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value)
        if value not in (None, "") and not isinstance(value, dict):
            return str(value)
    return ""


def _mcp_acts(name: str, given: dict, text: str, result: str) -> list[Act]:
    # The verb is the first action or read word, past a product prefix
    # ("slack_post_message", "github_create_issue").
    words = _words(name)
    server = name.split("__")[1].lower() if name.count("__") >= 2 else ""
    at = next((i for i, w in enumerate(words) if w in _ACTION_WORDS or w in _READ_WORDS), None)
    if at is None or words[at] in _READ_WORDS:
        # Creating or saving a draft does nothing the nouns below name;
        # sending one (send_draft) is a message.
        return []
    verb, nouns = words[at], set(words[at + 1 :])
    every = set(words) | set(re.split(r"[\W_]+", server))
    number = _given_target(given, _NUMBER_KEYS)
    number = f"#{number}" if number.isdigit() else ""
    family = next((w for w in ("github", "gitlab", "bitbucket") if w in every), "")
    if verb in ("comment", "review", "approve") or (
        verb in ("create", "add", "write", "post", "reply") and nouns & {"comment", "review"}
    ):
        return [Act(c.MESSAGE, number, text, f"comment {family}".strip())]
    if verb in ("send", "post", "reply", "respond", "forward", "dm", "notify", "chat") or (
        verb in ("create", "add", "write") and nouns & {"message", "reply", "post"}
    ):
        channel = next((scope for keys, scope in _MCP_CHANNELS if keys & every), "")
        family = next((w for w in sorted(every) if w in _MCP_CHANNELS[1][0] - {"chat"}), "")
        target = _given_target(given, _CHANNEL_KEYS) or _given_target(given, _RECIPIENT_KEYS)
        return [Act(c.MESSAGE, target, text, f"{channel} {family}".strip())]
    if verb == "merge":
        return [Act(c.MERGE, number, text)]
    if verb in ("create", "open") and nouns & {"pull", "pr", "merge"}:
        found = re.search(r'"number"\s*:\s*(\d+)|/pull/(\d+)', result)
        target = f"#{found.group(1) or found.group(2)}" if found else ""
        return [Act(c.PR, target, text, family)]
    if verb in ("create", "open", "file") and nouns & {"issue", "ticket", "bug"}:
        return [Act(c.ISSUE, "", text, family)]
    if verb in ("upload", "put"):
        return [Act(c.UPLOAD, "", text)]
    branch = str(given.get("branch") or "")
    if verb == "push":
        return [Act(c.PUSH, branch, text, "git"), Act(c.COMMIT, "", text)]
    if verb in ("commit",) or (verb in ("create", "update") and nouns & {"file", "files"}):
        return [Act(c.COMMIT, "", text)]
    if verb in ("deploy", "publish", "release") or (verb == "create" and "release" in nouns):
        if given.get("draft") is True:
            return []
        env = _env_of(" ".join(str(v) for v in given.values() if isinstance(v, str)))
        return [Act(c.PUBLISH, "", text, f"{family} {env}".strip())]
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
        if "Everything up-to-date" in result:
            return []
        return [Act(c.PUSH, str(given.get("branch") or ""), hay, "git")]
    if name == "git_push_pr":
        acts = [Act(c.PUSH, str(given.get("branch") or ""), hay, "git")]
        if "created PR" in result and not re.search(r"PR creation (?:failed|skipped)", result):
            # The pull request is the one the result names, never a number
            # in its title or body ("Closes #12").
            found = _URL_RE.search(result)
            acts.append(Act(c.PR, found.group(0) if found else "", result, "github"))
        return acts
    if name == "git_add_commit":
        sha = _SHA_IN_RE.search(result)
        return [Act(c.COMMIT, sha.group(0) if sha else "", hay)]
    if name in ("git_merge", "ws_merge_worktree"):
        return [Act(c.MERGE, str(given.get("branch") or given.get("name") or ""), hay)]
    if name == "github":
        return _gh_acts([str(a) for a in given.get("args") or []], hay, result)
    if name == "github_api_write":
        mutation = str(given.get("graphql_mutation") or "")
        if mutation:
            return [
                Act(kind, "", hay, scope) for p, kind, scope in _MUTATION_ACTS if p.search(mutation)
            ]
        return _api_acts(
            str(given.get("method") or "POST").upper(), str(given.get("endpoint") or ""), hay
        )
    if name in ("create_artifact", "edit_artifact"):
        return [Act(c.ARTIFACT, "", hay)]
    if name == "memory_write":
        return [Act(c.MEMORY, "", hay)]
    if name == "config_set":
        return [Act(c.SETTING, str(given.get("key") or ""), hay)]
    if name in ("cron_create", "cron_update"):
        return [Act(c.SCHEDULE, str(given.get("name") or ""), hay)]
    if name in ("send_message", "send_session_message"):
        to = str(given.get("to_agent_id") or given.get("to_session_id") or "")
        return [Act(c.MESSAGE, to, hay, "agent")]
    if name == "ws_delete_file":
        return [Act(c.REMOVED, str(given.get("path") or ""), hay)]
    if name in _COMMAND_KEYS:
        command = str(given.get(_COMMAND_KEYS[name]) or "")
        if name == "aws_cli" and not command.lstrip().startswith("aws "):
            command = f"aws {command}"
        return _command_acts(command, hay, result)
    if name == "run_python":
        code = str(given.get("code") or "")
        return [Act(k, "", hay, s) for k, pattern, s in _CODE_ACTS if pattern.search(code)]
    if name.startswith("mcp__"):
        return _mcp_acts(name, given, hay, result)
    return []


# ── files written ──────────────────────────────────────────────────────────

_PATH_KEYS = ("path", "destination_path", "file_path", "filename", "target_path", "output_path")
_REDIRECT_RE = re.compile(r"(?:^|[^<>&0-9])>{1,2}\s*([^\s;&|<>]+)")


def _at(cwd: str, target: str) -> str:
    """``target`` as a command in ``cwd`` names it ("" when the command's
    folder is unknown: the caller resolves it against the workspace)."""
    target = target.strip("'\"")
    if not cwd or target.startswith(("/", "~")):
        return target
    return os.path.normpath(os.path.join(cwd, target))


def _command_writes(command: str, cwd: str) -> list[str]:
    """The files a command line writes, each against the folder the command
    is in at that point ("cd out && pandoc a.md -o b.pdf" writes out/b.pdf)."""
    out: list[str] = []
    for part in split_subcommands(strip_heredoc_bodies(command or "")):
        words = next(iter(_argvs(part)), [])
        if words[:1] == ["cd"] and len(words) > 1 and words[1] != "-":
            cwd = _at(cwd, words[1]) if cwd else words[1]
            continue
        out += [_at(cwd, m.group(1)) for m in _REDIRECT_RE.finditer(part)]
        for argv in _argvs(part):
            argv = _unwrap(argv)
            prog, args = os.path.basename(argv[0]), argv[1:]
            out += [_at(cwd, args[i + 1]) for i, a in enumerate(args[:-1]) if a in _OUTPUT_FLAGS]
            rest = _positionals(args)
            if prog in _WRITERS and rest:
                out.append(_at(cwd, rest[-1]))
            if prog == "git" and rest[:1] == ["mv"] and len(rest) >= 3:
                out.append(_at(cwd, rest[-1]))
            if prog in ("touch", "tee", "mkdir"):
                out += [_at(cwd, p) for p in rest]
    return out


def writes_of(name: str, given: dict) -> list[str]:
    """The files one call writes, as it names them: a writing tool's path
    argument, a shell command's redirect targets, output flags and the
    destination of a copy or move, each against the folder the command ran
    in where it says so. A tool that only reads writes nothing."""
    if name in _COMMAND_KEYS:
        cwd = str(given.get("cwd") or "").strip()
        return _command_writes(str(given.get(_COMMAND_KEYS[name]) or ""), cwd)
    from server.goals.deliverables import OUTSIDE_WRITERS, _write_targets

    if name in OUTSIDE_WRITERS:
        return _write_targets(name, given)
    if reads_only(name):
        return []
    return [str(given[k]) for k in _PATH_KEYS if isinstance(given.get(k), str) and given[k].strip()]
