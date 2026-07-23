"""Shared authenticated `gh` runner for the GitHub tools.

`run_gh` is the ONE place a GitHub CLI command executes in the server process
(the authenticated path — file/keychain gh auth, NOT the ws_run_command sandbox,
which has no GitHub auth). It hardens the environment, forces non-interactive
mode, scrubs secrets from all output, and refuses to run a mutation unless the
caller passes `approved=True` — a backstop so no dispatch path can mutate GitHub
outside an approval executor.
"""

from __future__ import annotations

import os
import subprocess

from server.ci.provider import scrub_secrets  # reuse the tested secret scrubber
from server.git.gh_classify import FORBIDDEN, classify_github

# gh api flags that carry a request body; their presence with no explicit GET
# means gh switches the method to POST (a mutation).
_BODY_FLAGS = frozenset({"-f", "--field", "-F", "--raw-field", "--input"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def gh_env() -> dict:
    """os.environ hardened for a non-interactive, no-pager, no-color gh run.
    Auth stays file/keychain-based — no token is injected here."""
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["CLICOLOR"] = "0"
    env["PAGER"] = "cat"
    env.pop("GH_PAGER", None)
    env.pop("GH_DEBUG", None)  # never print request headers
    return env


def _api_is_mutation(args: list[str]) -> bool:
    """Conservative mutation check for `gh api` argv (args[0] == 'api')."""
    method = ""
    for i, tok in enumerate(args):
        if tok in ("-X", "--method") and i + 1 < len(args):
            method = args[i + 1].upper()
        elif tok.startswith("--method="):
            method = tok.split("=", 1)[1].upper()
    if method:
        return method in _MUTATING_METHODS
    # No explicit method: a body flag makes gh POST.
    return any(t in _BODY_FLAGS or t.split("=", 1)[0] in _BODY_FLAGS for t in args)


def _looks_like_mutation(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "api":
        return _api_is_mutation(args)
    return classify_github(args).is_mutation


def run_gh(
    args: list[str], *, cwd: str, timeout: int = 30, approved: bool = False
) -> tuple[int, str]:
    """Run `gh <args>` authenticated in the server process. argv only (no shell).

    Returns (returncode, scrubbed_combined_output). returncode is -1 when gh is
    missing or the subprocess could not start. Raises RuntimeError if asked to
    run a mutation without approved=True (a programming error — mutations must
    come from the approval executor)."""
    # FORBIDDEN governance actions never run — not even with approved=True.
    if args and args[0] != "api" and classify_github(args).kind == FORBIDDEN:
        raise RuntimeError(
            "run_gh refused a forbidden GitHub governance action (repo delete/transfer/visibility)."
        )
    if _looks_like_mutation(args) and not approved:
        raise RuntimeError(
            "run_gh refused a GitHub mutation without approval — mutations must "
            "route through the approval executor path."
        )
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=gh_env(),
        )
    except FileNotFoundError:
        return (
            -1,
            "'gh' CLI not found. Install it from https://cli.github.com/ and run `gh auth login`.",
        )
    except subprocess.TimeoutExpired:
        return -1, f"gh command timed out after {timeout}s."
    except Exception as e:  # noqa: BLE001 — surface any spawn failure
        return -1, f"gh command failed to run: {e}"
    return result.returncode, scrub_secrets((result.stdout + result.stderr).strip())
