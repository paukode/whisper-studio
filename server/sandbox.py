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

from server.security import egress_policy
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
    """Generate a macOS sandbox-exec profile that denies sensitive paths.

    When the active network_policy tier is non-permissive, this ALSO appends
    rules forcing outbound HTTP(S) through the egress proxy (see
    `_macos_network_restriction_rules`). Byte-for-byte identical to the
    pre-egress-policy output when the tier is permissive (today's default),
    verified by tests/test_egress_policy.py — no rules are appended in that
    case, not even an empty line.
    """
    deny_rules = []
    for path in _effective_denied_paths(allow_paths):
        if os.path.exists(path) or os.path.isdir(os.path.dirname(path)):
            escaped = path.replace('"', '\\"')
            deny_rules.append(f'(deny file-read* file-write* (subpath "{escaped}"))')
            deny_rules.append(f'(deny file-read* file-write* (literal "{escaped}"))')

    deny_block = "\n".join(deny_rules)

    profile = f"""\
(version 1)
(allow default)
{deny_block}
"""

    network_rules = _macos_network_restriction_rules()
    if network_rules:
        profile += "\n".join(network_rules) + "\n"

    return profile


def _macos_network_restriction_rules() -> list[str]:
    """Network-egress rules appended to the macOS profile ONLY when the
    active network_policy tier is non-permissive. Returns [] (no rules,
    hence no change to the profile at all) when permissive.

    Why this exists: HTTPS_PROXY/HTTP_PROXY (injected via `_merged_env`
    below) is an env var CONVENTION — a command that ignores it (a raw
    `socket.connect`, or any tool that doesn't consult those variables)
    would bypass server/security/egress_policy.py's domain allowlist
    entirely and reach the internet directly. These rules close that hole
    at the kernel level: deny all direct outbound connections on the two
    ports virtually everything HTTP-shaped uses (80, 443), so the ONLY way
    out is through the (allowlist-enforcing) local proxy.

    The proxy always binds to an OS-assigned ephemeral port (never 80/443 —
    see egress_policy.EgressProxy.__init__), so it is never itself caught by
    these deny rules; no explicit loopback allow-rule is needed alongside
    them (verified manually with `sandbox-exec` against this exact
    (allow default) + (deny ... 443) combination: the deny only matches
    connections whose remote port is 443, so an ephemeral-port loopback
    connection is unaffected and stays allowed by the general default).

    Residual gaps, spelled out rather than glossed over:
      - DNS (port 53) is untouched — see egress_policy.py's module
        docstring, point 3.
      - A command using a non-standard HTTPS port (rare) would bypass this
        specific port-443 deny; only 80/443 are covered, matching what the
        proxy itself listens for via HTTPS_PROXY/HTTP_PROXY.
      - Linux/bwrap enforcement is separate and weaker — see
        `_bwrap_network_restriction_args` for what's actually implemented
        there and its untested-on-this-machine caveat.
    """
    policy = egress_policy.get_active_policy()
    if policy.get("tier", egress_policy.TIER_PERMISSIVE) == egress_policy.TIER_PERMISSIVE:
        return []
    return [
        '(deny network-outbound (remote ip "*:443"))',
        '(deny network-outbound (remote ip "*:80"))',
    ]


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


def _bwrap_network_restriction_args() -> list[str]:
    """bwrap args enforcing the active network_policy tier on Linux.

    IMPLEMENTED BUT UNTESTED ON THIS PLATFORM. This change was written and
    verified on macOS (this repo's dev machine has no Linux box available);
    the bwrap path below has never actually been run. Treat it as a
    defensively-written first pass, not a verified guarantee — test it for
    real on Linux before relying on it.

    What IS implemented: `--unshare-net` when the tier is non-permissive.
    bwrap creates a network namespace containing only a loopback interface
    with no external connectivity — this is bwrap's own documented,
    unprivileged behavior (it needs no capabilities beyond the user
    namespace bwrap already uses to run without root), so this part should
    be reliable even unverified.

    What is deliberately NOT implemented: a veth pair bridging that isolated
    namespace back to the local egress proxy so curated-tier traffic could
    still reach allowlisted domains (mirroring the macOS story: kernel-level
    "*:443/*:80" deny + a working proxy path). Building that correctly needs
    one of:
      - CAP_NET_ADMIN on the HOST network namespace to create the host side
        of a veth pair — this process runs unprivileged by design, and
        granting it that capability is a materially bigger change than this
        task's scope, or
      - an external unprivileged bridging helper (slirp4netns / pasta, as
        used by rootless containers for exactly this problem) — plausible,
        but wiring one in means guessing at its exact CLI surface and PID/
        netns attach sequencing with zero ability to verify any of it here.
        Shipping that guess would risk a subtly-broken bridge that LOOKS
        like allowlist filtering but silently isn't — worse than being
        upfront that it isn't there yet.

    Net effect: on Linux, any non-permissive tier currently behaves like
    "restrictive" (zero egress) rather than a domain-filtered allowlist.
    That is a real gap versus the tier's stated intent, but it fails CLOSED
    (no network at all) rather than open (unfiltered network) — the safe
    direction to be wrong in. Follow-up: implement + test a real
    slirp4netns/pasta bridge on an actual Linux host.
    """
    policy = egress_policy.get_active_policy()
    if policy.get("tier", egress_policy.TIER_PERMISSIVE) == egress_policy.TIER_PERMISSIVE:
        return []
    return ["--unshare-net"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# Credentials that must NEVER reach a sandboxed child. The sandbox profile is
# `(allow default)` with network open, so an inherited GitHub token could be
# exfiltrated by a prompt-injected command (e.g. `curl "…?t=$GH_TOKEN"`) —
# blocking `gh` would not stop that, since it is a non-gh command reading an env
# var. GitHub auth for the authenticated git/github tools is file/keychain-based
# (~/.config/gh/hosts.yml, itself sandbox-denied), so nothing legitimate in the
# sandbox needs these.
_SANDBOX_ENV_DENYLIST = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"})


def _network_proxy_env_vars() -> dict[str, str]:
    """HTTPS_PROXY/HTTP_PROXY vars pointing at the local egress-filtering
    proxy (server/security/egress_policy.py), or {} when the active
    network_policy tier is "permissive" (today's default — no proxy is even
    started in that case, let alone injected). Both-case variable names are
    set since different tools check different casings."""
    policy = egress_policy.get_active_policy()
    if policy.get("tier", egress_policy.TIER_PERMISSIVE) == egress_policy.TIER_PERMISSIVE:
        return {}
    host, port = egress_policy.ensure_proxy_running()
    proxy_url = f"http://{host}:{port}"
    return {
        "HTTPS_PROXY": proxy_url,
        "https_proxy": proxy_url,
        "HTTP_PROXY": proxy_url,
        "http_proxy": proxy_url,
    }


def _merged_env(env_extra: dict | None) -> dict:
    """The subprocess environment: os.environ (minus credentials that must never
    reach a sandboxed child, see _SANDBOX_ENV_DENYLIST), plus proxy env vars
    when a non-permissive network_policy is active (see
    _network_proxy_env_vars — a no-op dict when permissive), plus any caller
    extras (applied last, so a caller can still override either of the above
    if it ever needs to).

    Always returns an explicit dict rather than None-to-inherit, so the denylist
    is enforced even when the caller passes no extras."""
    base = {k: v for k, v in os.environ.items() if k not in _SANDBOX_ENV_DENYLIST}
    base.update(_network_proxy_env_vars())
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
    """Run command under bubblewrap (Linux).

    Network restriction (--unshare-net when a non-permissive network_policy
    is active) is best-effort and UNTESTED on this platform — see
    _bwrap_network_restriction_args's docstring for exactly what that does
    and does not cover.
    """
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
        *_bwrap_network_restriction_args(),
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
) -> tuple[subprocess.Popen, str | None]:
    """Start a sandbox-wrapped process streaming combined stdout/stderr to a file.

    Unlike ``run_sandboxed`` this does NOT block on completion — it returns the
    live ``Popen`` so callers can wait with their own budget and hand the
    process off to a background waiter on timeout (the anti-restart handoff in
    server/tasks/handoff.py). Same deny-profile as ``run_sandboxed``.

    Returns ``(proc, profile_path)``. ``profile_path`` (macOS only) must be
    unlinked by the caller once the process exits — the profile file has to
    outlive the process, exactly the ``build_pty_sandbox_wrap`` contract.

    Egress policy note: this path picks up the SAME network-restriction
    rules as ``run_sandboxed`` (the macOS profile via ``_generate_macos_profile``,
    and ``--unshare-net`` on bwrap when non-permissive — see
    ``_macos_network_restriction_rules`` / ``_bwrap_network_restriction_args``).
    It does NOT set ``env=`` at all, though (pre-existing — this function
    inherits the full parent environment unconditionally, unlike
    ``run_sandboxed``'s ``_merged_env``), so it never gets the
    HTTPS_PROXY/HTTP_PROXY injection either. Under a non-permissive tier
    that means background/streaming commands started this way get NO
    network at all rather than a filtered allowlist — fails closed, not
    open, but worth knowing if this ever surprises someone.
    """
    from server.process_utils import new_process_group

    profile_path: str | None = None
    if _is_sandbox_exec_available():
        profile = _generate_macos_profile(cwd)
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
            *_bwrap_network_restriction_args(),
        ]
        for path in _effective_denied_paths(None):
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
