"""Exhaustive tests for the GitHub tool classifier (the security heart).

These pin the DENY / REDIRECT / READ / WRITE / DANGER decisions and the raw-API
endpoint containment. A regression here is a security regression.
"""

import pytest

from server.git.gh_classify import (
    DANGER,
    DENY,
    READ,
    REDIRECT,
    WRITE,
    classify_api,
    classify_github,
    validate_api_endpoint,
)


@pytest.mark.parametrize(
    "args,kind",
    [
        (["pr", "list"], READ),
        (["pr", "view", "2"], READ),
        (["pr", "diff", "2"], READ),
        (["issue", "list"], READ),
        (["run", "view", "123"], READ),
        (["release", "download", "v1"], READ),
        (["search", "issues", "foo"], READ),
        (["status"], READ),
        (["pr"], READ),  # family with no verb → help/list
    ],
)
def test_reads(args, kind):
    assert classify_github(args).kind == kind


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "create", "--title", "x"],
        ["pr", "close", "2"],
        ["pr", "comment", "2", "--body", "hi"],
        ["issue", "create", "--title", "x"],
        ["release", "create", "v1"],
        ["run", "rerun", "1"],
        ["workflow", "run", "ci.yml"],
        ["label", "create", "bug"],
    ],
)
def test_writes(args):
    assert classify_github(args).kind == WRITE


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "merge", "2"],
        ["issue", "delete", "5"],
        ["issue", "transfer", "5", "other/repo"],
        # repo delete/transfer are FORBIDDEN now (see test_gh_forbidden.py); rename/archive stay DANGER.
        ["repo", "rename", "new"],
        ["repo", "archive", "o/r"],
        ["release", "delete", "v1"],
        ["run", "delete", "1"],
        ["cache", "delete", "1"],
        ["project", "item-delete"],
    ],
)
def test_dangers(args):
    assert classify_github(args).kind == DANGER


@pytest.mark.parametrize(
    "family",
    [
        "auth",
        "config",
        "secret",
        "variable",
        "ssh-key",
        "gpg-key",
        "extension",
        "alias",
        "codespace",
        "completion",
    ],
)
def test_denylist(family):
    assert classify_github([family, "list"]).kind == DENY
    assert classify_github([family, "token"]).kind == DENY


def test_redirects():
    assert classify_github(["api", "repos/o/r"]).kind == REDIRECT
    assert classify_github(["api", "repos/o/r"]).redirect_to == "github_api"
    assert classify_github(["repo", "clone", "o/r"]).kind == REDIRECT
    assert classify_github(["repo", "clone", "o/r"]).redirect_to == "git_clone"


def test_unknown_verb_fails_closed_to_write():
    # A verb not in any table (new/unknown upstream subcommand) must be
    # approval-gated, never inline.
    assert classify_github(["pr", "frobnicate"]).kind == WRITE
    assert classify_github(["newfamily", "whatever"]).kind == WRITE


def test_empty_is_denied():
    assert classify_github([]).kind == DENY
    assert classify_github(["--help"]).kind == DENY  # no positional family


def test_leading_flags_skipped():
    assert classify_github(["-R", "o/r", "pr", "list"]).kind == READ
    assert classify_github(["-R", "o/r", "pr", "merge", "2"]).kind == DANGER


# --- gh api method classification ---


def test_api_method_classification():
    assert classify_api("GET", has_body=False).kind == READ
    assert classify_api("HEAD", has_body=False).kind == READ
    assert classify_api("", has_body=False).kind == READ  # defaults to GET
    assert classify_api("", has_body=True).kind == WRITE  # body → POST
    assert classify_api("POST", has_body=True).kind == WRITE
    assert classify_api("PATCH", has_body=True).kind == WRITE
    assert classify_api("DELETE", has_body=False).kind == DANGER


# --- endpoint containment ---


@pytest.mark.parametrize(
    "ep",
    [
        "user/tokens",
        "applications/123/token",
        "repos/o/r/actions/secrets",
        "repos/o/r/actions/secrets/FOO",
        "repos/o/r/environments/prod/secrets",
        "user/keys",
        "repos/o/r/keys",
        "app/installations/1/access_tokens",
        "scim/v2/organizations/o/Users",
        "authorizations",
    ],
)
def test_endpoint_denylist(ep):
    assert validate_api_endpoint(ep) is not None


@pytest.mark.parametrize(
    "ep",
    [
        "repos/{owner}/{repo}/issues",
        "repos/o/r/pulls/2",
        "user",
        "graphql",
        "repos/o/r/actions/runs",
    ],
)
def test_endpoint_allowed(ep):
    assert validate_api_endpoint(ep) is None


def test_endpoint_traversal_and_absolute():
    assert validate_api_endpoint("../../etc/passwd") is not None
    assert validate_api_endpoint("-X") is not None
    assert validate_api_endpoint("https://evil.example/x") is not None
    assert validate_api_endpoint("https://api.github.com/repos/o/r") is None
    assert validate_api_endpoint("") is not None
