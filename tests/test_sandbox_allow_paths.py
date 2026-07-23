"""run_sandboxed's allow_paths lets cloud-credential tools reach ~/.aws while
every other secret stays denied; build_pty_sandbox_wrap wraps a PTY shell under
sandbox-exec on macOS (and is a no-op elsewhere)."""

import os

from server.sandbox import (
    _DENIED_PATHS,
    _effective_denied_paths,
    _is_sandbox_exec_available,
    build_pty_sandbox_wrap,
)


def test_allow_paths_none_keeps_full_denylist():
    assert _effective_denied_paths(None) == _DENIED_PATHS


def test_allow_paths_excludes_only_allowed():
    aws_real = os.path.realpath(os.path.expanduser("~/.aws"))
    ssh_real = os.path.realpath(os.path.expanduser("~/.ssh"))
    eff_real = {os.path.realpath(p) for p in _effective_denied_paths(["~/.aws"])}
    assert aws_real not in eff_real, "~/.aws should be allowed through"
    assert ssh_real in eff_real, "~/.ssh must still be denied"


def test_pty_sandbox_wrap():
    shell = ["/bin/bash", "--norc"]
    argv, profile = build_pty_sandbox_wrap(shell, "/tmp")
    if _is_sandbox_exec_available():
        assert argv[0] == "sandbox-exec"
        assert argv[-2:] == shell
        assert profile and os.path.exists(profile)
        os.unlink(profile)
    else:
        assert argv == shell
        assert profile is None
