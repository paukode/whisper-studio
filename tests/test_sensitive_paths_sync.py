"""The sandbox and command validator must agree on the shared denylist core."""

import os

from server.sandbox import _DENIED_PATHS
from server.security.command_validator import validate_command
from server.security.sensitive_paths import (
    ABSOLUTE_PATHS,
    HOME_PATHS,
    SANDBOX_ONLY_HOME_PATHS,
    VALIDATOR_ONLY_ABSOLUTE_PATHS,
)


def test_shared_core_present_in_sandbox_denylist():
    for p in HOME_PATHS:
        assert os.path.expanduser(f"~/{p}") in _DENIED_PATHS, (
            f"shared core path {p} missing from sandbox denylist"
        )
    for p in ABSOLUTE_PATHS:
        assert p in _DENIED_PATHS


def test_no_duplicates_across_lists():
    combined = HOME_PATHS + SANDBOX_ONLY_HOME_PATHS + ABSOLUTE_PATHS + VALIDATOR_ONLY_ABSOLUTE_PATHS
    assert len(combined) == len(set(combined))
    assert len(_DENIED_PATHS) == len(set(_DENIED_PATHS))


def test_validator_blocks_shared_core_paths():
    for cmd in [
        "cat ~/.ssh/id_rsa",
        "cat ~/.kube/config",
        "cat ~/.aws/credentials",
        "cat /etc/shadow x",
        "grep secret /etc/passwd",
        "cat ~/.config/git/credentials",
    ]:
        assert validate_command(cmd) is not None, f"{cmd!r} should be blocked"


def test_validator_passes_lookalikes():
    for cmd in [
        "aws s3 ls",
        "ls ~/.awsome",
        "echo docker ps",
        "kubectl get pods",
    ]:
        assert validate_command(cmd) is None, f"{cmd!r} should pass"


def test_sandbox_only_entries_still_denied():
    # Mirrors what test_sandbox_denylist relies on: the high-value
    # sandbox-only entries survive the centralization.
    for p in [
        "~/Library/Keychains",
        "~/.zsh_history",
        "~/.password-store",
        "~/.config/gh/hosts.yml",
    ]:
        assert os.path.expanduser(p) in _DENIED_PATHS
