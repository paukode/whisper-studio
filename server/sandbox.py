"""
OS-level command sandboxing — restricts filesystem access for shell commands.

Uses platform-native sandboxing:
  - macOS: sandbox-exec with a deny profile
  - Linux: bubblewrap (bwrap) if available
  - Fallback: no OS-level sandbox (command validators still apply)

The sandbox denies read/write access to sensitive paths (SSH keys, cloud
credentials, system secrets) while allowing access to the workspace and
standard system directories.
"""

import logging
import os
import platform
import shutil
import subprocess
import tempfile

from server.security.sensitive_paths import expanded_sandbox_paths

log = logging.getLogger("whisper-studio")

# ---------------------------------------------------------------------------
# Denied paths — sensitive directories and files
# ---------------------------------------------------------------------------

# Canonical list lives in server/security/sensitive_paths.py so the OS
# sandbox and the command validator cannot drift on the shared core.
_DENIED_PATHS = expanded_sandbox_paths()

# ---------------------------------------------------------------------------
# macOS sandbox profile generation
# ---------------------------------------------------------------------------


def _effective_denied_paths(allow_paths: list[str] | None) -> list[str]:
    """The deny-list minus any path the caller explicitly allows. Used so
    cloud-credential tools (aws_cli, boto3 run_python) can reach ~/.aws while
    every other secret (~/.ssh, git creds, /etc/shadow, …) stays blocked."""
    if not allow_paths:
        return _DENIED_PATHS
    allowed_real = {os.path.realpath(os.path.expanduser(p)) for p in allow_paths}
    return [p for p in _DENIED_PATHS if os.path.realpath(p) not in allowed_real]


def _generate_macos_profile(workspace: str, allow_paths: list[str] | None = None) -> str:
    """Generate a macOS sandbox-exec profile that denies sensitive paths."""
    deny_rules = []
    for path in _effective_denied_paths(allow_paths):
        if os.path.exists(path) or os.path.isdir(os.path.dirname(path)):
            escaped = path.replace('"', '\\"')
            deny_rules.append(f'(deny file-read* file-write* (subpath "{escaped}"))')
            deny_rules.append(f'(deny file-read* file-write* (literal "{escaped}"))')

    deny_block = "\n".join(deny_rules)

    return f"""\
(version 1)
(allow default)
{deny_block}
"""


def _bwrap_deny_args(path: str) -> list[str]:
    """bwrap args to make an existing path inaccessible. A directory is shadowed
    with an empty tmpfs; a file is masked with /dev/null (``--tmpfs`` only works
    on directory mountpoints, so file entries like ~/.npmrc need ro-bind)."""
    if os.path.isdir(path):
        return ["--tmpfs", path]
    return ["--ro-bind", os.devnull, path]


def _is_sandbox_exec_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


def _is_bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_sandbox_available() -> bool:
    """Check if any OS-level sandboxing is available."""
    return _is_sandbox_exec_available() or _is_bwrap_available()


# Credentials that must NEVER reach a sandboxed child. The sandbox profile is
# `(allow default)` with network open, so an inherited GitHub token could be
# exfiltrated by a prompt-injected command (e.g. `curl "…?t=$GH_TOKEN"`) —
# blocking `gh` would not stop that, since it is a non-gh command reading an env
# var. GitHub auth for the authenticated git/github tools is file/keychain-based
# (~/.config/gh/hosts.yml, itself sandbox-denied), so nothing legitimate in the
# sandbox needs these.
_SANDBOX_ENV_DENYLIST = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"})


def _merged_env(env_extra: dict | None) -> dict:
    """The subprocess environment: os.environ (minus credentials that must never
    reach a sandboxed child, see _SANDBOX_ENV_DENYLIST) plus any caller extras.

    Always returns an explicit dict rather than None-to-inherit, so the denylist
    is enforced even when the caller passes no extras."""
    base = {k: v for k, v in os.environ.items() if k not in _SANDBOX_ENV_DENYLIST}
    if env_extra:
        base.update({str(k): str(v) for k, v in env_extra.items()})
    return base


def run_sandboxed(
    command: str,
    *,
    cwd: str,
    timeout: int = 60,
    capture_output: bool = True,
    text: bool = True,
    allow_paths: list[str] | None = None,
    input_data: str | None = None,
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run a command with OS-level sandboxing if available.

    ``allow_paths`` removes specific entries from the deny-list (e.g. ``~/.aws``
    for the AWS tools, which need their credentials to function). ``env_extra``
    adds variables to the subprocess environment (passed via ``env=``, NOT
    prepended to the command — a command prefix would corrupt any command that
    begins with a shell compound construct like ``if``/``for``/``case``).

    Falls back to plain subprocess.run if no sandbox is available.
    """
    if _is_sandbox_exec_available():
        return _run_macos_sandboxed(
            command,
            cwd=cwd,
            timeout=timeout,
            capture_output=capture_output,
            text=text,
            allow_paths=allow_paths,
            input_data=input_data,
            env_extra=env_extra,
        )
    if _is_bwrap_available():
        return _run_bwrap_sandboxed(
            command,
            cwd=cwd,
            timeout=timeout,
            capture_output=capture_output,
            text=text,
            allow_paths=allow_paths,
            input_data=input_data,
            env_extra=env_extra,
        )
    # Fallback: no OS-level sandbox
    from server.process_utils import kill_process_group, new_process_group

    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        preexec_fn=new_process_group,
        env=_merged_env(env_extra),
    )
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        raise
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _run_macos_sandboxed(
    command: str,
    *,
    cwd: str,
    timeout: int,
    capture_output: bool,
    text: bool,
    allow_paths: list[str] | None = None,
    input_data: str | None = None,
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run command under macOS sandbox-exec."""
    profile = _generate_macos_profile(cwd, allow_paths)

    # Write profile to temp file (sandbox-exec needs a file path)
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="whisper_sandbox_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(profile)

        from server.process_utils import kill_process_group, new_process_group

        sandboxed_command = [
            "sandbox-exec",
            "-f",
            profile_path,
            "/bin/sh",
            "-c",
            command,
        ]
        proc = subprocess.Popen(
            sandboxed_command,
            cwd=cwd,
            stdin=subprocess.PIPE if input_data is not None else None,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
            preexec_fn=new_process_group,
            env=_merged_env(env_extra),
        )
        try:
            stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            raise
        return subprocess.CompletedProcess(sandboxed_command, proc.returncode, stdout, stderr)
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass


def _run_bwrap_sandboxed(
    command: str,
    *,
    cwd: str,
    timeout: int,
    capture_output: bool,
    text: bool,
    allow_paths: list[str] | None = None,
    input_data: str | None = None,
    env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run command under bubblewrap (Linux)."""
    bwrap_args = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",  # read-only root
        "--bind",
        cwd,
        cwd,  # read-write workspace
        "--bind",
        "/tmp",
        "/tmp",  # read-write tmp
        "--dev",
        "/dev",  # device nodes
        "--proc",
        "/proc",  # proc filesystem
    ]

    # Deny sensitive paths by making them inaccessible
    for path in _effective_denied_paths(allow_paths):
        if os.path.exists(path):
            bwrap_args.extend(_bwrap_deny_args(path))

    bwrap_args.extend(
        [
            "--chdir",
            cwd,
            "/bin/sh",
            "-c",
            command,
        ]
    )

    from server.process_utils import kill_process_group, new_process_group

    proc = subprocess.Popen(
        bwrap_args,
        cwd=cwd,
        stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        preexec_fn=new_process_group,
        env=_merged_env(env_extra),
    )
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        raise
    return subprocess.CompletedProcess(bwrap_args, proc.returncode, stdout, stderr)


def popen_sandboxed(
    command: str,
    *,
    cwd: str,
    stdout_file,
    allow_paths: list[str] | None = None,
) -> tuple[subprocess.Popen, str | None]:
    """Start a sandbox-wrapped process streaming combined stdout/stderr to a file.

    Unlike ``run_sandboxed`` this does NOT block on completion — it returns the
    live ``Popen`` so callers can wait with their own budget and hand the
    process off to a background waiter on timeout (the anti-restart handoff in
    server/tasks/handoff.py). Same deny-profile as ``run_sandboxed``.

    Returns ``(proc, profile_path)``. ``profile_path`` (macOS only) must be
    unlinked by the caller once the process exits — the profile file has to
    outlive the process, exactly the ``build_pty_sandbox_wrap`` contract.
    """
    from server.process_utils import new_process_group

    profile_path: str | None = None
    if _is_sandbox_exec_available():
        profile = _generate_macos_profile(cwd, allow_paths)
        fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="whisper_bg_sandbox_")
        with os.fdopen(fd, "w") as f:
            f.write(profile)
        argv: list | str = ["sandbox-exec", "-f", profile_path, "/bin/sh", "-c", command]
        shell = False
    elif _is_bwrap_available():
        argv = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            cwd,
            cwd,
            "--bind",
            "/tmp",
            "/tmp",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        for path in _effective_denied_paths(allow_paths):
            if os.path.exists(path):
                argv.extend(_bwrap_deny_args(path))
        argv.extend(["--chdir", cwd, "/bin/sh", "-c", command])
        shell = False
    else:
        argv = command
        shell = True

    try:
        proc = subprocess.Popen(
            argv,
            shell=shell,
            cwd=cwd,
            stdout=stdout_file,
            stderr=subprocess.STDOUT,
            preexec_fn=new_process_group,
        )
    except Exception:
        if profile_path:
            try:
                os.unlink(profile_path)
            except OSError:
                pass
        raise
    return proc, profile_path


def build_pty_sandbox_wrap(shell_cmd: list[str], cwd: str) -> tuple[list[str], str | None]:
    """Wrap an interactive shell command in macOS sandbox-exec so a long-lived
    PTY shell (terminal_run's hidden sandbox session) runs under the same
    filesystem deny-list as run_sandboxed.

    Returns ``(argv, profile_path)``. The caller MUST ``os.unlink(profile_path)``
    once the process exits (the profile file must outlive the shell, unlike the
    one-shot run_sandboxed which deletes it immediately).

    On non-macOS, or when sandbox-exec is unavailable, returns
    ``(shell_cmd, None)`` unchanged — the PTY runs unsandboxed and command
    validation remains the only enforcement layer.
    """
    if not _is_sandbox_exec_available():
        return shell_cmd, None
    profile = _generate_macos_profile(cwd)
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="whisper_pty_sandbox_")
    with os.fdopen(fd, "w") as f:
        f.write(profile)
    return ["sandbox-exec", "-f", profile_path, *shell_cmd], profile_path
