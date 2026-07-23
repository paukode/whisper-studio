"""Pure, table-driven classification for the GitHub tools.

`classify_github(args)` decides, from a `gh` argv (no leading "gh"), whether an
invocation is DENY / REDIRECT / READ / WRITE / DANGER / FORBIDDEN. It is a pure
function so it can be unit-tested exhaustively and re-evaluated on the approved
payload before execution. `classify_api(method, has_body)` does the same for the
raw `gh api` plane by HTTP method, and `check_api_governance` marks catastrophic
governance mutations (repo delete/transfer/visibility, people/team changes) that
are never permitted.

Security posture: unknown (family, verb) pairs FAIL CLOSED to WRITE (approval
gated, never inline), so a new upstream subcommand degrades to human-gated
rather than silently running as a read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DENY = "deny"
REDIRECT = "redirect"
READ = "read"
WRITE = "write"
DANGER = "danger"
FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class Verdict:
    kind: str  # one of DENY / REDIRECT / READ / WRITE / DANGER / FORBIDDEN
    reason: str = ""
    redirect_to: str = ""

    @property
    def is_read(self) -> bool:
        return self.kind == READ

    @property
    def is_mutation(self) -> bool:
        return self.kind in (WRITE, DANGER)


# Catastrophic, irreversible governance actions that NO session, agent, or
# subagent may ever perform — not even with explicit human approval. The user
# must do these on GitHub directly. Enforced as FORBIDDEN (never executes, never
# offered for approval), a strictly harder block than DANGER (which asks).
_FORBIDDEN_MESSAGE = (
    "This action ({what}) is never permitted from the assistant — deleting or "
    "transferring a repository, changing its visibility, or adding/removing "
    "people or teams is too risky to automate. Do it yourself on GitHub."
)


# Layer 0 — first-token denylist. Hard reject, never approvable. Each of these
# either prints a credential, mutates credentials/config, or installs/rewrites
# executable behaviour.
_DENY_FAMILIES = frozenset(
    {
        "auth",  # `gh auth token` prints the token
        "config",  # `gh config get -h github.com oauth_token` prints the token
        "secret",
        "variable",
        "ssh-key",
        "gpg-key",
        "extension",  # installs arbitrary executable code
        "alias",  # persistent command rewriting could remap a read onto a write
        "codespace",  # ssh / port-forward channels
        "completion",
    }
)

# Layer 1 — verb tables keyed (family, verb). Anything not listed for a known
# family falls through to WRITE (fail-closed). Families not present here at all
# also fall through to WRITE.
_READ: dict[str, frozenset[str]] = {
    "pr": frozenset({"list", "view", "diff", "checks", "status"}),
    "issue": frozenset({"list", "view", "status"}),
    "repo": frozenset({"list", "view"}),
    "release": frozenset({"list", "view", "download"}),
    "run": frozenset({"list", "view", "watch", "download"}),
    "workflow": frozenset({"list", "view"}),
    "label": frozenset({"list"}),
    "gist": frozenset({"list", "view", "clone"}),
    "project": frozenset({"list", "view", "item-list", "field-list"}),
    "cache": frozenset({"list"}),
}

_DANGER: dict[str, frozenset[str]] = {
    "pr": frozenset({"merge"}),
    "issue": frozenset({"delete", "transfer"}),
    "repo": frozenset({"delete", "rename", "archive", "unarchive"}),
    "release": frozenset({"delete", "delete-asset"}),
    "run": frozenset({"delete"}),
    "label": frozenset({"delete"}),
    "gist": frozenset({"delete"}),
    "project": frozenset({"delete", "item-delete"}),
    "cache": frozenset({"delete"}),
}

# Families whose bare form (no verb) or every subcommand is a read.
_READ_ONLY_FAMILIES = frozenset({"search", "status"})

# repo verbs that are FORBIDDEN outright (delete / ownership transfer).
_REPO_FORBIDDEN_VERBS = frozenset({"delete", "transfer"})


def _has_visibility_flag(args: list[str]) -> bool:
    return any(a == "--visibility" or a.startswith("--visibility=") for a in args)


# Global flags that take a separate value and can appear BEFORE the subcommand.
# Their value must not be mistaken for the family (e.g. `-R o/r pr list`). The
# `--flag=value` form embeds its value and needs no skip. Subcommand-level value
# flags appear only after (family, verb), which are already resolved by then.
_VALUE_FLAGS = frozenset({"-R", "--repo", "--hostname", "-H"})


def _first_positional(args: list[str]) -> tuple[str | None, str | None]:
    """Return (family, verb): the first two positional tokens, skipping flags
    and the values of known value-taking global flags. gh's grammar is
    `gh <family> <verb> [args]`; both may be absent.

    Imperfect argv parsing here fails SAFE: a misread family/verb that isn't in
    the READ/DANGER tables falls through to WRITE (approval-gated), never inline.
    """
    positionals: list[str] = []
    skip_next = False
    for tok in args:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if tok in _VALUE_FLAGS:
                skip_next = True
            continue
        positionals.append(tok)
        if len(positionals) >= 2:
            break
    family = positionals[0] if positionals else None
    verb = positionals[1] if len(positionals) > 1 else None
    return family, verb


def classify_github(args: list[str]) -> Verdict:
    """Classify a `gh` argv (WITHOUT the leading 'gh')."""
    if not args:
        return Verdict(DENY, reason="empty command")
    family, verb = _first_positional(args)
    if family is None:
        return Verdict(DENY, reason="no gh subcommand")

    # Layer 0: denylist + redirects.
    if family in _DENY_FAMILIES:
        return Verdict(
            DENY,
            reason=f"'gh {family}' is blocked (credential/config/extension surface).",
        )
    if family == "api":
        return Verdict(
            REDIRECT,
            reason="use the github_api / github_api_write tools for raw REST/GraphQL.",
            redirect_to="github_api",
        )
    if family == "repo" and verb == "clone":
        return Verdict(
            REDIRECT,
            reason="use the git_clone tool (it validates the URL and connects the workspace).",
            redirect_to="git_clone",
        )

    # Catastrophic repo governance — FORBIDDEN, never approvable, never executed.
    if family == "repo" and verb in _REPO_FORBIDDEN_VERBS:
        return Verdict(FORBIDDEN, reason=_FORBIDDEN_MESSAGE.format(what=f"gh repo {verb}"))
    if family == "repo" and verb == "edit" and _has_visibility_flag(args):
        return Verdict(
            FORBIDDEN, reason=_FORBIDDEN_MESSAGE.format(what="changing repository visibility")
        )

    # Bare read-only families (search/*, bare status).
    if family in _READ_ONLY_FAMILIES:
        return Verdict(READ)

    # Layer 1: verb table.
    if verb is None:
        # A family with no verb (e.g. `gh pr`) is a help/listing no-op — treat
        # as read.
        return Verdict(READ)
    if verb in _DANGER.get(family, frozenset()):
        return Verdict(DANGER)
    if verb in _READ.get(family, frozenset()):
        return Verdict(READ)
    # Unknown (family, verb): fail closed to WRITE (approval-gated, never inline).
    return Verdict(WRITE)


def classify_api(method: str, *, has_body: bool) -> Verdict:
    """Classify a raw `gh api` call by HTTP method.

    GET/HEAD → READ. DELETE → DANGER. Other explicit methods → WRITE. With no
    explicit method, gh defaults to GET unless a body field is present, in which
    case it switches to POST."""
    m = (method or "").upper()
    if not m:
        m = "POST" if has_body else "GET"
    if m in ("GET", "HEAD"):
        return Verdict(READ)
    if m == "DELETE":
        return Verdict(DANGER)
    if m in ("POST", "PUT", "PATCH"):
        return Verdict(WRITE)
    # Unknown method: fail closed.
    return Verdict(WRITE)


# Raw-API endpoint containment. These substrings mark credential/secret/key
# surfaces that the family-level `gh secret`/`gh auth` denial does NOT cover
# when reached through `gh api`. Applied to BOTH read and write.
_API_DENY_SUBSTRINGS = (
    "user/tokens",
    "applications/",
    "/secrets",  # actions / dependabot / codespaces / environment secrets
    "secrets/",
    "user/keys",
    "/keys",  # repos/*/keys and friends
    "access_tokens",  # app installation access-token minting
    "scim/",
    "authorizations",
)

_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9_.\-/{}]+$")


def validate_api_endpoint(endpoint: str) -> str | None:
    """Return an error string if a `gh api` endpoint is unsafe, else None.

    Rejects absolute URLs other than api.github.com, path traversal, invalid
    characters, and any path touching the credential/secret/key surfaces in
    _API_DENY_SUBSTRINGS. `graphql` is allowed (its safety is decided by the
    operation-type check in the executor)."""
    if not endpoint or not isinstance(endpoint, str):
        return "endpoint is required"
    ep = endpoint.strip()
    if ep == "graphql":
        return None
    # Absolute URL: only api.github.com, and reduce to its path for the rest.
    if "://" in ep:
        low = ep.lower()
        if not (
            low.startswith("https://api.github.com/") or low.startswith("http://api.github.com/")
        ):
            return "absolute URLs are not allowed; pass a path, or use api.github.com only"
        ep = ep.split("://", 1)[1].split("/", 1)[1] if "/" in ep.split("://", 1)[1] else ""
    ep = ep.lstrip("/")
    if ep.startswith("-"):
        return "endpoint may not start with '-'"
    if ".." in ep:
        return "endpoint may not contain '..'"
    # Strip a query string before the strict path-charset check; `gh api`
    # callers normally pass params via -f/-F rather than inline query.
    path = ep.split("?", 1)[0]
    if not _ENDPOINT_RE.match(path):
        return "endpoint contains invalid characters"
    low = ep.lower()
    for bad in _API_DENY_SUBSTRINGS:
        if bad in low:
            return f"endpoint targets a blocked credential/secret surface ('{bad}')"
    return None


def _api_path_segments(endpoint: str) -> list[str]:
    ep = (endpoint or "").strip()
    if "://" in ep:
        rest = ep.split("://", 1)[1]
        ep = rest.split("/", 1)[1] if "/" in rest else ""
    ep = ep.lstrip("/").split("?", 1)[0].lower()
    return [s for s in ep.split("/") if s]


def check_api_governance(
    method: str, endpoint: str, body: dict | None = None, fields: dict | None = None
) -> str | None:
    """Return a reason string if a `gh api` MUTATION is a FORBIDDEN catastrophic
    governance action (repo delete/transfer, visibility change, add/remove
    people or teams), else None. Reads (GET/HEAD) are never governance-blocked."""
    m = (method or "").upper()
    if m in ("GET", "HEAD"):
        return None
    segs = _api_path_segments(endpoint)
    if not segs:
        return None
    keys = {str(k).lower() for k in (body or {})} | {str(k).lower() for k in (fields or {})}

    def _forbid(what: str) -> str:
        return _FORBIDDEN_MESSAGE.format(what=what)

    # Repo delete: DELETE /repos/{owner}/{repo}
    if m == "DELETE" and len(segs) == 3 and segs[0] == "repos":
        return _forbid("deleting a repository")
    # Ownership transfer: POST /repos/{owner}/{repo}/transfer
    if segs[-1] == "transfer" and "repos" in segs:
        return _forbid("transferring repository ownership")
    # Visibility change: mutate /repos/{owner}/{repo} with private/visibility.
    if len(segs) == 3 and segs[0] == "repos" and ({"private", "visibility"} & keys):
        return _forbid("changing repository visibility")
    # Add/remove people: repo collaborators, org/team memberships.
    if "collaborators" in segs:
        return _forbid("adding or removing a repository collaborator")
    if "memberships" in segs or "member" in segs:
        return _forbid("adding or removing an org/team member")
    # Any mutating call touching a team (team CRUD, repo-team access grant).
    if "teams" in segs:
        return _forbid("changing a team or its repository access")
    return None
