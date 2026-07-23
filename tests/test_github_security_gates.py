"""Security gates that ship with the GitHub tools (spec §1.3, §1.4):

- Destructive GitHub mutations always require explicit approval, even under
  bypass / autopilot / blanket session approval.
- `gh` is refused in the command sandbox (its authenticated replacement is the
  github / github_api tools).
"""

from server.security.command_validator import validate_command
from server.security.permissions import MODE_BYPASS, MODE_DEFAULT, resolve_static_decision


def _decide(category, mode=MODE_DEFAULT, approvals=None):
    return resolve_static_decision(
        "github", {}, category, approvals or {}, mode, auto_allow_trusted=False
    )


def test_destructive_always_asks():
    # No mode, no blanket, no trusted flag may auto-approve a destructive op.
    assert _decide("github-destructive") == "ask"
    assert _decide("github-destructive", mode=MODE_BYPASS) == "ask"
    assert _decide("github-destructive", approvals={"github-destructive": "allow"}) == "ask"
    assert (
        resolve_static_decision(
            "github", {}, "github-destructive", {}, MODE_BYPASS, auto_allow_trusted=True
        )
        == "ask"
    )


def test_routine_github_follows_normal_rules():
    # The non-destructive category is a normal ask-by-default category.
    assert _decide("github") == "ask"
    assert _decide("github", mode=MODE_BYPASS) == "allow"
    assert _decide("github", approvals={"github": "allow"}) == "allow"


def test_gh_blocked_in_sandbox():
    assert validate_command("gh pr close 2") is not None
    assert validate_command("gh api repos/o/r") is not None
    assert validate_command("GH_TOKEN=x gh pr list") is not None  # env-prefixed
    assert validate_command("/opt/homebrew/bin/gh pr list") is not None  # absolute path
    assert validate_command("sudo gh auth status") is not None  # wrapper


def test_non_gh_commands_still_allowed():
    assert validate_command("git status") is None
    assert validate_command("ls -la") is None
    assert validate_command("echo gh") is None  # 'gh' not the program
    assert validate_command("night build") is None  # substring, not the program
