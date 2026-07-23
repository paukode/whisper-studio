"""Executors for the GitHub hybrid tools: `github` (verb plane) and
`github_api` / `github_api_write` (raw REST/GraphQL plane).

Reads run inline in the authenticated server process; mutations return the
[WS_APPROVAL] sentinel and only execute from the approval executor (do_*),
which refuses unattended subagent runs and verifies the resulting state.
"""

from __future__ import annotations

import json
import os
import tempfile

from server.approval.spec import refuse_if_agent
from server.executors import register_executor
from server.git.gh_classify import (
    DANGER,
    DENY,
    FORBIDDEN,
    REDIRECT,
    check_api_governance,
    classify_api,
    classify_github,
    validate_api_endpoint,
)
from server.git.gh_common import run_gh
from server.workspace import get_workspace_path

_LIST_VERBS = frozenset({"list"})
_READ_STRIP_FLAGS = frozenset({"--web", "-w"})

# (family, verb) -> (read_verb, expected_state | None) for verify-after-mutate.
_VERIFY: dict[tuple[str, str], tuple[str, str | None]] = {
    ("pr", "close"): ("view", "CLOSED"),
    ("pr", "reopen"): ("view", "OPEN"),
    ("pr", "merge"): ("view", "MERGED"),
    ("issue", "close"): ("view", "CLOSED"),
    ("issue", "reopen"): ("view", "OPEN"),
}


def _cwd() -> tuple[str | None, str | None]:
    ws = get_workspace_path()
    if not ws:
        return None, "No workspace connected. Connect a workspace first."
    return ws, None


def _positionals(args: list[str]) -> list[str]:
    from server.git.gh_classify import _VALUE_FLAGS

    out: list[str] = []
    skip = False
    for tok in args:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            if tok in _VALUE_FLAGS:
                skip = True
            continue
        out.append(tok)
    return out


# --------------------------------------------------------------------------
# `github` verb tool
# --------------------------------------------------------------------------


def _read_hygiene(args: list[str]) -> list[str]:
    """Strip side-effecting flags and bound list output."""
    out = [a for a in args if a not in _READ_STRIP_FLAGS]
    fam = _positionals(out)
    if len(fam) >= 2 and fam[1] in _LIST_VERBS and not any(a in ("-L", "--limit") for a in out):
        out = [*out, "--limit", "30"]
    return out


@register_executor("github", read_only=False, concurrent_safe=False)
def _exec_github(tool_input, transcript, current_attachments):
    cwd, err = _cwd()
    if err:
        return f"Error: {err}"
    session_id = tool_input.pop("__session_id__", "")
    args = tool_input.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return "Error: args must be a list of strings (gh argv without the leading 'gh')."
    v = classify_github(args)
    if v.kind == FORBIDDEN:
        return f"Error: {v.reason}"
    if v.kind == DENY:
        return f"Error: {v.reason}"
    if v.kind == REDIRECT:
        return f"Error: {v.reason}"
    if v.is_read:
        rc, out = run_gh(_read_hygiene(args), cwd=cwd, timeout=_timeout(tool_input, args))
        return out if rc == 0 else f"Error: {out}"
    action = "github_destructive" if v.kind == DANGER else "github"
    return "[WS_APPROVAL]" + json.dumps({"action": action, "args": args, "session_id": session_id})


def do_github(payload: dict) -> tuple[bool, str]:
    refusal = refuse_if_agent(payload, what="A GitHub mutation")
    if refusal:
        return False, refusal.error
    cwd, err = _cwd()
    if err:
        return False, err
    args = payload.get("args") or []
    v = classify_github(args)  # re-validate the round-tripped payload
    if v.kind == FORBIDDEN:
        return False, v.reason
    if v.kind in (DENY, REDIRECT):
        return False, f"Rejected on re-validation: {v.reason}"
    rc, out = run_gh(args, cwd=cwd, timeout=60, approved=True)
    if rc != 0:
        return False, f"gh {' '.join(args[:2])} failed: {out}"
    return _verify_github(args, cwd, out)


def _verify_github(args: list[str], cwd: str, mutation_out: str) -> tuple[bool, str]:
    pos = _positionals(args)
    if len(pos) < 2:
        return True, mutation_out or "Done."
    family, verb = pos[0], pos[1]
    spec = _VERIFY.get((family, verb))
    if not spec:
        # rc==0 from authenticated gh is itself meaningful for create/edit/etc.
        return True, mutation_out or f"gh {family} {verb} completed."
    read_verb, expected = spec
    ident = pos[2] if len(pos) > 2 else None
    read_args = [family, read_verb, *([ident] if ident else []), "--json", "state,url"]
    rc, out = run_gh(read_args, cwd=cwd, timeout=30)
    if rc != 0:
        return True, f"{mutation_out}\n[state could not be re-verified: {out}]".strip()
    try:
        state = json.loads(out).get("state")
    except Exception:
        return True, f"{mutation_out}\n[state unverified]".strip()
    if expected is None:
        return True, f"{family} {verb} done (state now {state})."
    if state == expected:
        return True, f"Verified: {family} #{ident} is now {state}."
    return False, f"{family} #{ident} is {state} after {verb} (expected {expected})."


# --------------------------------------------------------------------------
# `github_api` (read) / `github_api_write` (mutation)
# --------------------------------------------------------------------------


def _timeout(tool_input: dict, args: list[str]) -> int:
    try:
        t = int(tool_input.get("timeout_seconds") or 30)
    except (TypeError, ValueError):
        t = 30
    return max(5, min(120, t))


def _build_api_args(payload: dict, *, body_tmp: str | None) -> list[str]:
    """Construct a `gh api` argv from structured input. The server owns argv[1]
    ('api'), so no subcommand smuggling is possible."""
    args = ["api", payload["endpoint"]]
    method = (payload.get("method") or "").upper()
    if method:
        args += ["--method", method]
    # GraphQL query/mutation as a single -f field.
    gql = payload.get("graphql_query") or payload.get("graphql_mutation")
    if gql:
        args += ["-f", f"query={gql}"]
        for k, val in (payload.get("graphql_vars") or {}).items():
            args += ["-F", f"{k}={val}"]
    # Query params for reads.
    for k, val in (payload.get("fields") or {}).items():
        args += ["-f", f"{k}={val}"]
    # JSON body via a server-written tempfile (model cannot point --input at a
    # local file — closes the @file exfil).
    if body_tmp:
        args += ["--input", body_tmp]
    return args


def _graphql_is_read(gql: str) -> bool:
    s = (gql or "").lstrip()
    # Positively identify a single query; anything else (mutation, multi-op,
    # uninspectable) is NOT a read.
    return s.startswith("{") or s.startswith("query")


@register_executor("github_api", read_only=True, concurrent_safe=True)
def _exec_github_api(tool_input, transcript, current_attachments):
    cwd, err = _cwd()
    if err:
        return f"Error: {err}"
    tool_input.pop("__session_id__", "")
    endpoint = tool_input.get("endpoint", "")
    ep_err = validate_api_endpoint(endpoint)
    if ep_err:
        return f"Error: {ep_err}"
    method = (tool_input.get("method") or "").upper()
    if method and method not in ("GET", "HEAD"):
        return "Error: github_api is read-only; use github_api_write for POST/PATCH/PUT/DELETE."
    gql = tool_input.get("graphql_query")
    if gql and not _graphql_is_read(gql):
        return "Error: that GraphQL is not a single read query; use github_api_write for mutations."
    has_body = bool(tool_input.get("fields") or gql)
    if not classify_api(method, has_body=has_body).is_read:
        return "Error: this call is not a read; use github_api_write."
    args = _build_api_args(tool_input, body_tmp=None)
    rc, out = run_gh(args, cwd=cwd, timeout=_timeout(tool_input, args))
    return out if rc == 0 else f"Error: {out}"


@register_executor("github_api_write", read_only=False, concurrent_safe=False)
def _exec_github_api_write(tool_input, transcript, current_attachments):
    cwd, err = _cwd()
    if err:
        return f"Error: {err}"
    session_id = tool_input.pop("__session_id__", "")
    endpoint = tool_input.get("endpoint", "")
    ep_err = validate_api_endpoint(endpoint)
    if ep_err:
        return f"Error: {ep_err}"
    method = (tool_input.get("method") or "").upper()
    if not method and not tool_input.get("graphql_mutation"):
        return "Error: method is required (POST, PATCH, PUT, or DELETE)."
    gov = check_api_governance(
        method or "POST", endpoint, tool_input.get("body"), tool_input.get("fields")
    )
    if gov:
        return f"Error: {gov}"
    v = classify_api(method or "POST", has_body=True)
    if v.is_read:
        return "Error: this looks like a read; use github_api."
    action = "github_api_write_destructive" if v.kind == DANGER else "github_api_write"
    return "[WS_APPROVAL]" + json.dumps(
        {
            "action": action,
            "endpoint": endpoint,
            "method": method,
            "body": tool_input.get("body"),
            "fields": tool_input.get("fields"),
            "graphql_mutation": tool_input.get("graphql_mutation"),
            "graphql_vars": tool_input.get("graphql_vars"),
            "session_id": session_id,
        }
    )


def do_github_api_write(payload: dict) -> tuple[bool, str]:
    refusal = refuse_if_agent(payload, what="A GitHub API mutation")
    if refusal:
        return False, refusal.error
    cwd, err = _cwd()
    if err:
        return False, err
    ep_err = validate_api_endpoint(payload.get("endpoint", ""))  # re-validate
    if ep_err:
        return False, f"Rejected on re-validation: {ep_err}"
    gov = check_api_governance(
        payload.get("method", ""),
        payload.get("endpoint", ""),
        payload.get("body"),
        payload.get("fields"),
    )
    if gov:
        return False, gov
    body = payload.get("body")
    body_tmp = None
    try:
        if body is not None:
            fd, body_tmp = tempfile.mkstemp(suffix=".json", prefix="ghapi_")
            with os.fdopen(fd, "w") as f:
                json.dump(body, f)
        args = _build_api_args(payload, body_tmp=body_tmp)
        rc, out = run_gh(args, cwd=cwd, timeout=60, approved=True)
    finally:
        if body_tmp and os.path.exists(body_tmp):
            os.remove(body_tmp)
    if rc != 0:
        return (
            False,
            f"API {payload.get('method') or 'call'} {payload.get('endpoint')} failed: {out}",
        )
    return _verify_api(payload, cwd, out)


def _verify_api(payload: dict, cwd: str, mutation_out: str) -> tuple[bool, str]:
    method = (payload.get("method") or "").upper()
    endpoint = payload.get("endpoint", "")
    if payload.get("graphql_mutation"):
        # GraphQL responses are the mutation's own output, not an independent
        # read — mark UNVERIFIED honestly.
        return (
            True,
            f"{mutation_out}\n[GraphQL mutation sent — state not independently verified]".strip(),
        )
    if method == "DELETE":
        rc, _ = run_gh(["api", endpoint], cwd=cwd, timeout=30)
        if rc != 0:
            return True, f"Verified: {endpoint} no longer exists (deleted)."
        return False, f"{endpoint} still exists after DELETE."
    # Re-GET the resource by its returned url when present.
    try:
        url = json.loads(mutation_out).get("url") if mutation_out else None
    except Exception:
        url = None
    if url:
        rc, out = run_gh(["api", url], cwd=cwd, timeout=30)
        if rc == 0:
            return True, f"Verified via re-read.\n{mutation_out}".strip()
    return True, f"{mutation_out}\n[mutation sent — state not independently re-verified]".strip()
