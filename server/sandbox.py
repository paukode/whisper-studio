"""
OS-level command sandboxing: restricts filesystem access for shell commands.

Uses macOS sandbox-exec with a deny profile. It is the only backend: when
sandbox-exec cannot be used, run_sandboxed, popen_sandboxed and
build_pty_sandbox_wrap raise SandboxUnavailableError and nothing runs (fail
closed). Nothing that goes through this module runs without the sandbox,
except a Bypass turn's commands: under server/security/bypass_sandbox.py's
scope every entry point runs the command with ``/bin/sh -c`` and no profile,
and sandbox-exec is not needed (the user's rule: Bypass works like Claude
Code's bypass). The environment stays the same in both cases.

The sandbox denies read/write access to sensitive paths (SSH keys, cloud
credentials, system secrets) while allowing access to the workspace and
standard system directories.

Secret reads: by default the deny-list blocks reading AND writing. A run
with ``secret_reads=True`` (a Bypass-mode call, or one a person approved
with ``secret_access``, see server/security/secret_reads.py) may read the
credential stores (the Keychain for ``security`` and ``codesign``, ~/.ssh,
~/.aws and the rest) but still never writes them.

Three write modes:
  - "open" (default): writes allowed everywhere except the secret deny-list.
    Human-approved commands keep it: the approval card is the control, and
    flows like saving a document to ~/Documents keep working.
  - "workspace": Codex-style default-deny: reads stay open (minus secrets)
    but writes are confined to the command's working tree (plus the git dirs
    a linked worktree needs), the calling session's own scratch folder,
    system temp, and explicit write_paths. Package caches (npm, pip, uv) are
    pointed into that scratch folder, since their usual home-folder places
    are outside the confinement. Applied
    to agent-stamped (unattended) commands, where no human reviews each
    command: the OS, not the prompt, bounds the blast radius.
  - "readonly": every write denied except /dev (a read-only classified
    command, so a write its classifier missed is refused by the OS).
"""

import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile

from server.security import bypass_sandbox, egress_policy
from server.security.sensitive_env import GITHUB_ENV_VARS
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
    cloud-credential tools (aws_cli, boto3 run_python) can read ~/.aws while
    every other secret (~/.ssh, git creds, /etc/shadow, …) stays blocked. An
    allowed path stays read-only (_credential_write_rules)."""
    if not allow_paths:
        return _DENIED_PATHS
    allowed_real = {os.path.realpath(os.path.expanduser(p)) for p in allow_paths}
    return [p for p in _DENIED_PATHS if os.path.realpath(p) not in allowed_real]


def _git_write_roots(cwd: str) -> list[str]:
    """Write roots a linked git WORKTREE needs beyond its own tree.

    A linked worktree's ``.git`` is a FILE pointing at the primary repo's
    ``.git/worktrees/<name>`` (index, HEAD live there), whose ``commondir``
    in turn points at the shared ``.git`` (objects, refs). Under
    workspace-write both must stay writable or ``git add``/``git stash``
    inside an isolated agent worktree fails on a read-only object store.
    """
    roots: list[str] = []
    dotgit = os.path.join(cwd, ".git")
    try:
        if os.path.isfile(dotgit):
            with open(dotgit) as f:
                first = f.readline().strip()
            if first.startswith("gitdir:"):
                gitdir = os.path.realpath(os.path.join(cwd, first.split(":", 1)[1].strip()))
                roots.append(gitdir)
                commondir_file = os.path.join(gitdir, "commondir")
                if os.path.isfile(commondir_file):
                    with open(commondir_file) as f:
                        common = f.read().strip()
                    roots.append(os.path.realpath(os.path.join(gitdir, common)))
    except OSError:
        pass
    return roots


# Always-writable system scratch roots under workspace-write: per-user temp
# ($TMPDIR lives under /var/folders on macOS), the classic /tmp trees, and
# /dev (tty, null). Both the /X and /private/X spellings are listed — Seatbelt
# matches the path as given, and callers see either through the symlinks.
_WORKSPACE_WRITE_SYSTEM_ROOTS = [
    "/tmp",
    "/private/tmp",
    "/var/tmp",
    "/private/var/tmp",
    "/var/folders",
    "/private/var/folders",
    "/dev",
]


def _workspace_write_roots(tree: str | None, scratch_dir: str | None = None) -> list[str]:
    """The write-allowed subpaths for ``write_mode="workspace"``: the tree the
    run may write in (plus the git dirs a linked worktree needs) when there is
    one, the calling session's own scratch folder (where the prompt sends
    throwaway files, virtualenvs and package caches), and system temp. A
    caller's write_paths (the AWS CLI caches) come later, in
    _credential_write_rules."""
    roots = [os.path.realpath(tree), *_git_write_roots(tree)] if tree else []
    if scratch_dir:
        roots.append(os.path.realpath(scratch_dir))
    roots.extend(_WORKSPACE_WRITE_SYSTEM_ROOTS)
    seen: set[str] = set()
    return [r for r in roots if not (r in seen or seen.add(r))]


def _spellings(path: str) -> list[str]:
    """``path`` with ~ expanded, and its resolved form when that differs:
    Seatbelt matches the path as given, and a caller may reach either."""
    given = os.path.abspath(os.path.expanduser(path))
    real = os.path.realpath(given)
    return [given] if real == given else [given, real]


def _credential_write_rules(
    allow_paths: list[str] | None, write_paths: list[str] | None
) -> list[str]:
    """SBPL rules that keep every allowed credential folder read-only, apart
    from the caller's write_paths beneath it (the AWS CLI caches).

    The aws CLI and boto3 run programs that files in ~/.aws name
    (credential_process, "!" CLI aliases, plugins), so a write there is code
    that a later AWS call runs, boto3 in the server process included. These
    rules go LAST, after the workspace write allows, so a workspace that
    contains ~/.aws (the home folder) cannot open it again."""
    rules = []
    for path in allow_paths or []:
        for spelling in _spellings(path):
            escaped = spelling.replace('"', '\\"')
            rules.append(f'(deny file-write* (subpath "{escaped}"))')
    for path in write_paths or []:
        for spelling in _spellings(path):
            escaped = spelling.replace('"', '\\"')
            rules.append(f'(allow file-write* (subpath "{escaped}"))')
    return rules


def scratch_cache_env(scratch_dir: str) -> dict[str, str]:
    """Environment that sends npm, pip and uv to package caches inside the
    session's scratch folder. Their default caches (~/.npm, ~/Library/Caches)
    are outside a confined run's write roots, so without this an install
    fails on its own cache, and the model asks for full filesystem access."""
    cache = os.path.join(scratch_dir, "cache")
    return {
        "npm_config_cache": os.path.join(cache, "npm"),
        "PIP_CACHE_DIR": os.path.join(cache, "pip"),
        "UV_CACHE_DIR": os.path.join(cache, "uv"),
    }


def _scratch_env(write_mode: str, scratch_dir: str | None) -> dict[str, str]:
    """The package-cache environment for a confined run, else nothing: an open
    run can write the user's own caches, and a read-only one writes nowhere."""
    if write_mode != "workspace" or not scratch_dir:
        return {}
    return scratch_cache_env(scratch_dir)


def _control_port_deny_rules() -> list[str]:
    """Deny a CONFINED command the app's own control API ports on loopback.

    A command that no person reviewed runs write-confined, but the OS sandbox
    does not restrict the network by default, so it could reach
    http://127.0.0.1:<port>/api/approval/execute and run another command with
    OPEN writes (escaping its own confinement), or flip the permission mode.
    Denying the loopback ports that reach the backend closes that, while any
    other local port (a dev server, a database) stays reachable. The ports
    come from the environment: ``PORT`` (the backend, default 41430) and, in
    ``setup.sh --dev``, ``WHISPER_DEV_PROXY_PORT`` (the Vite server, which
    proxies /api to the backend). Open (human-approved) commands are exempt:
    a person read that exact command."""
    ports = [(os.environ.get("PORT") or "41430").strip()]
    proxy = (os.environ.get("WHISPER_DEV_PROXY_PORT") or "").strip()
    if proxy:
        ports.append(proxy)
    return [
        f'(deny network-outbound (remote ip "localhost:{port}"))'
        for port in ports
        if port.isdigit()
    ]


def _app_config_targets() -> tuple[list[str], list[str]]:
    """The app's own grant and config files and directories (server.
    infrastructure.paths.app_config_paths / app_config_dirs), each as written
    and as resolved, so a write-confined command (the kind that no person
    reviewed) never changes them even when its tree holds the data root or the
    user root. A file tool's write to one follows the permission mode instead
    (server/security/permissions.py, the app-config table)."""
    from server.infrastructure.paths import app_config_dirs, app_config_paths

    def _both(path: str) -> list[str]:
        real = os.path.realpath(path)
        return [path, real] if real != path else [path]

    files: list[str] = []
    for path in app_config_paths():
        files.extend(_both(path))
    dirs: list[str] = []
    for path in app_config_dirs():
        dirs.extend(_both(path))
    return list(dict.fromkeys(files)), list(dict.fromkeys(dirs))


def _generate_macos_profile(
    workspace: str | None,
    allow_paths: list[str] | None = None,
    write_mode: str = "open",
    scratch_dir: str | None = None,
    write_paths: list[str] | None = None,
    secret_reads: bool = False,
) -> str:
    """Generate a macOS sandbox-exec profile that denies sensitive paths.

    ``secret_reads`` turns every secret deny rule into a write-only deny: the
    run reads the credential stores but never changes them.

    ``allow_paths`` re-allows deny-listed credential folders for READING;
    they stay read-only except ``write_paths`` beneath them, in "open" and
    "workspace" mode alike (_credential_write_rules, the last rules).

    ``write_mode="workspace"`` additionally denies ALL writes and then allows
    them back only under the roots from ``_workspace_write_roots`` (the
    session's ``scratch_dir`` among them); a ``workspace`` of None writes no
    tree, only the scratch folder and temp. Rule ORDER
    is load-bearing (a later SBPL rule overrides an earlier match): the global
    write deny comes first, the writable roots after it, and the secret deny
    rules LAST — so a workspace that contains (or is) the home directory can
    never re-expose ~/.ssh and friends through its own write allow.

    ``write_mode="readonly"`` denies ALL writes and allows back only /dev — a
    PTY shell must still write to its own slave tty device, and /dev/null is
    universally assumed writable. Everything else, including the workspace and
    temp, stays read-only.

    When the active network_policy tier is non-permissive, this ALSO appends
    rules forcing outbound HTTP(S) through the egress proxy (see
    `_macos_network_restriction_rules`). With write_mode="open" the output is
    byte-for-byte identical to the output without an egress policy when the
    tier is permissive (the default), verified by
    tests/test_sandbox_egress_wiring.py: no rules are appended in that case,
    not even an empty line.
    """
    deny_rules = []
    denied = "file-write*" if secret_reads else "file-read* file-write*"
    for path in _effective_denied_paths(allow_paths):
        if os.path.exists(path) or os.path.isdir(os.path.dirname(path)):
            escaped = path.replace('"', '\\"')
            deny_rules.append(f'(deny {denied} (subpath "{escaped}"))')
            deny_rules.append(f'(deny {denied} (literal "{escaped}"))')

    deny_block = "\n".join(deny_rules)

    if write_mode == "workspace":
        write_allows = []
        for root in _workspace_write_roots(workspace, scratch_dir):
            escaped = root.replace('"', '\\"')
            write_allows.append(f'(allow file-write* (subpath "{escaped}"))')
        write_allow_block = "\n".join(write_allows)
        # After the allows, and so overriding them: a workspace that holds the
        # data root or the user root cannot re-allow the app's own grant and
        # config files (_app_config_targets). A file tool's write to one still
        # follows the permission mode; this only stops a confined command.
        cfg_files, cfg_dirs = _app_config_targets()
        config_rules = [
            f'(deny file-write* (literal "{p.replace(chr(34), chr(92) + chr(34))}"))'
            for p in cfg_files
        ] + [
            f'(deny file-write* (subpath "{p.replace(chr(34), chr(92) + chr(34))}"))'
            for p in cfg_dirs
        ]
        config_denies = "\n".join(config_rules)
        profile = f"""\
(version 1)
(allow default)
(deny file-write*)
{write_allow_block}
{config_denies}
{deny_block}
"""
    elif write_mode == "readonly":
        profile = f"""\
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "/dev"))
{deny_block}
"""
    else:
        profile = f"""\
(version 1)
(allow default)
{deny_block}
"""

    if write_mode != "readonly":
        credential_rules = _credential_write_rules(allow_paths, write_paths)
        if credential_rules:
            profile += "\n".join(credential_rules) + "\n"

    # A confined command (one no person reviewed) cannot reach the app's own
    # control API port, so it cannot drive an approval to escape its
    # confinement. Open (human-approved) commands keep full loopback access.
    if write_mode != "open":
        control_denies = _control_port_deny_rules()
        if control_denies:
            profile += "\n".join(control_denies) + "\n"

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
    """
    policy = egress_policy.get_active_policy()
    if policy.get("tier", egress_policy.TIER_PERMISSIVE) == egress_policy.TIER_PERMISSIVE:
        return []
    return [
        '(deny network-outbound (remote ip "*:443"))',
        '(deny network-outbound (remote ip "*:80"))',
    ]


def _is_sandbox_exec_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SANDBOX_UNAVAILABLE_MESSAGE = (
    "The command was NOT run: the macOS sandbox (sandbox-exec) is not available "
    "on this machine, and Whisper Studio never runs a command without it."
)


class SandboxUnavailableError(RuntimeError):
    """sandbox-exec cannot be used, so the command was refused and nothing ran.

    Raised by run_sandboxed, popen_sandboxed and build_pty_sandbox_wrap in
    every write_mode: an "open" run still relies on the profile's credential
    deny-list, so there is no unsandboxed fallback.
    A Bypass turn's command is not a fallback: it never needs the sandbox
    (see server/security/bypass_sandbox.py)."""

    def __init__(self, message: str = SANDBOX_UNAVAILABLE_MESSAGE):
        super().__init__(message)


def _require_sandbox_exec() -> None:
    """Raise SandboxUnavailableError unless sandbox-exec can be used."""
    if not _is_sandbox_exec_available():
        raise SandboxUnavailableError()


# Credentials that must NEVER reach a sandboxed child. The sandbox profile is
# `(allow default)` with network open, so an inherited GitHub token could be
# exfiltrated by a prompt-injected command (e.g. `curl "…?t=$GH_TOKEN"`) —
# blocking `gh` would not stop that, since it is a non-gh command reading an env
# var. GitHub auth for the authenticated git/github tools is file/keychain-based
# (~/.config/gh/hosts.yml, itself sandbox-denied), so nothing legitimate in the
# sandbox needs these. Canonical list lives in server/security/sensitive_env.py;
# deliberately ONLY the GitHub subset — see that module's docstring for why the
# cloud keys must keep passing here.
_SANDBOX_ENV_DENYLIST = GITHUB_ENV_VARS


def _network_proxy_env_vars() -> dict[str, str]:
    """HTTPS_PROXY/HTTP_PROXY vars pointing at the local egress-filtering
    proxy (server/security/egress_policy.py), or {} when the active
    network_policy tier is "permissive" (the default: no proxy is even
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


class _Cwd:
    """The default ``write_root`` of run_sandboxed: the run's own working
    directory. A marker, since None already means "no tree"."""


_CWD = _Cwd()


def _resolve_write_root(write_root: str | None | _Cwd, cwd: str) -> str | None:
    """The tree a "workspace" run may write in: ``cwd`` for the default, else
    ``write_root`` as given (None: no tree)."""
    return cwd if isinstance(write_root, _Cwd) else write_root


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
    write_mode: str = "open",
    scratch_dir: str | None = None,
    write_root: str | None | _Cwd = _CWD,
    write_paths: list[str] | None = None,
    secret_reads: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command under the macOS sandbox (sandbox-exec).

    ``secret_reads`` lets the run read the credential stores on the
    deny-list, never write them (see the module docstring).

    ``allow_paths`` removes specific entries from the deny-list (e.g. ``~/.aws``
    for the AWS tools, which need their credentials to function); they stay
    read-only. ``write_paths`` are folders beneath them that the run may write
    (the AWS CLI caches); each must exist, or the macOS sandbox refuses to
    create it. ``env_extra``
    adds variables to the subprocess environment (passed via ``env=``, NOT
    prepended to the command — a command prefix would corrupt any command that
    begins with a shell compound construct like ``if``/``for``/``case``).

    ``write_mode="workspace"`` confines WRITES to the working tree + temp +
    write_paths (see the module docstring).
    ``scratch_dir`` is the calling session's own scratch folder: writable under
    "workspace", and home to the run's package caches (scratch_cache_env).
    ``write_root`` is the tree a "workspace" run may write in. It defaults to
    ``cwd``; None writes no tree, only the scratch folder, temp and
    write_paths (aws_cli with no workspace connected, whose cwd is $HOME).

    Raises SandboxUnavailableError, and runs nothing, when sandbox-exec
    cannot be used (outside a Bypass turn, which runs with no sandbox and
    returns no profile path).
    """
    env_extra = {**_scratch_env(write_mode, scratch_dir), **(env_extra or {})}
    if bypass_sandbox.active():
        return _run_command(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            timeout=timeout,
            capture_output=capture_output,
            text=text,
            input_data=input_data,
            env_extra=env_extra,
        )
    _require_sandbox_exec()
    tree = _resolve_write_root(write_root, cwd)
    return _run_macos_sandboxed(
        command,
        cwd=cwd,
        timeout=timeout,
        capture_output=capture_output,
        text=text,
        allow_paths=allow_paths,
        input_data=input_data,
        env_extra=env_extra,
        write_mode=write_mode,
        scratch_dir=scratch_dir,
        write_root=tree,
        write_paths=write_paths,
        secret_reads=secret_reads,
    )


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
    write_mode: str = "open",
    scratch_dir: str | None = None,
    write_root: str | None = None,
    write_paths: list[str] | None = None,
    secret_reads: bool = False,
) -> subprocess.CompletedProcess:
    """Run command under macOS sandbox-exec. ``write_root`` is the resolved
    tree a "workspace" run may write in (None: no tree)."""
    profile = _generate_macos_profile(
        write_root,
        allow_paths,
        write_mode=write_mode,
        scratch_dir=scratch_dir,
        write_paths=write_paths,
        secret_reads=secret_reads,
    )

    # Write profile to temp file (sandbox-exec needs a file path)
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="whisper_sandbox_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(profile)

        return _run_command(
            ["sandbox-exec", "-f", profile_path, "/bin/sh", "-c", command],
            cwd=cwd,
            timeout=timeout,
            capture_output=capture_output,
            text=text,
            input_data=input_data,
            env_extra=env_extra,
        )
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass


def _run_command(
    argv: list[str],
    *,
    cwd: str,
    timeout: int,
    capture_output: bool,
    text: bool,
    input_data: str | None,
    env_extra: dict | None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` in its own process group with the merged environment,
    killing the group on timeout. Both the sandboxed and the Bypass run
    end here."""
    from server.process_utils import kill_process_group, new_process_group

    proc = subprocess.Popen(
        argv,
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
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def popen_sandboxed(
    command: str,
    *,
    cwd: str,
    stdout_file,
    write_mode: str = "open",
    write_root: str | None = None,
    scratch_dir: str | None = None,
    secret_reads: bool = False,
) -> tuple[subprocess.Popen, str | None]:
    """Start a sandbox-wrapped process streaming combined stdout/stderr to a file.

    Unlike ``run_sandboxed`` this does NOT block on completion — it returns the
    live ``Popen`` so callers can wait with their own budget and hand the
    process off to a background waiter on timeout (the anti-restart handoff in
    server/tasks/handoff.py). Same deny-profile as ``run_sandboxed``.

    Returns ``(proc, profile_path)``. ``profile_path`` must be
    unlinked by the caller once the process exits — the profile file has to
    outlive the process, exactly the ``build_pty_sandbox_wrap`` contract.
    Raises SandboxUnavailableError, and starts nothing, when sandbox-exec
    cannot be used (outside a Bypass turn, which runs with no sandbox and
    returns no profile path).

    Environment and egress: the child gets the same ``_merged_env`` as a
    ``run_sandboxed`` child, so the credential denylist holds (no GH_TOKEN for
    a prompt-injected ``curl "...?t=$GH_TOKEN"``) and, under a non-permissive
    network tier, the HTTPS_PROXY/HTTP_PROXY vars point at the allowlisting
    proxy. It also picks up the same network-restriction rules (the profile
    via ``_generate_macos_profile``), so the proxy is the only way out on
    ports 80 and 443. Approved and read-only ws_run_command runs, their 30 s handoff and every background shell task
    start here, so this is where the parity has to hold.

    ``write_mode`` is the ``run_sandboxed`` one: a read-only ws_run_command
    passes "readonly", so a write its classifier missed is refused by the OS.
    ``write_root`` is the tree a "workspace" run may write in; it defaults to
    ``cwd``. A ws_run_command passes the connected workspace, so a session
    that cd'd out of it (the cwd tracker follows a ``cd``) is still confined
    to the workspace, not to wherever it went. ``scratch_dir`` is the
    session's own scratch folder, and ``secret_reads`` the credential-store
    reads, as in ``run_sandboxed``.
    The output file is opened here, outside the sandbox, so output still flows.
    """
    from server.process_utils import new_process_group

    if bypass_sandbox.active():
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=cwd,
            stdout=stdout_file,
            stderr=subprocess.STDOUT,
            preexec_fn=new_process_group,
            env=_merged_env(_scratch_env(write_mode, scratch_dir)),
        )
        return proc, None
    _require_sandbox_exec()
    profile = _generate_macos_profile(
        write_root or cwd,
        write_mode=write_mode,
        scratch_dir=scratch_dir,
        secret_reads=secret_reads,
    )
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="whisper_bg_sandbox_")
    with os.fdopen(fd, "w") as f:
        f.write(profile)
    argv = ["sandbox-exec", "-f", profile_path, "/bin/sh", "-c", command]

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=stdout_file,
            stderr=subprocess.STDOUT,
            preexec_fn=new_process_group,
            env=_merged_env(_scratch_env(write_mode, scratch_dir)),
        )
    except Exception:
        try:
            os.unlink(profile_path)
        except OSError:
            pass
        raise
    return proc, profile_path


def build_pty_sandbox_wrap(
    shell_cmd: list[str],
    write_root: str | None,
    write_mode: str = "open",
    scratch_dir: str | None = None,
    secret_reads: bool = False,
) -> tuple[list[str], str | None]:
    """Wrap an interactive shell command in macOS sandbox-exec so a long-lived
    PTY shell (terminal_run's hidden sandbox session) runs under the same
    filesystem deny-list as run_sandboxed — and, for agent-stamped runs
    (``write_mode="workspace"``), the same default-deny write confinement,
    with the session's own ``scratch_dir`` writable. The caller starts the
    shell with the ``run_sandboxed`` environment (``_merged_env`` over
    ``_scratch_env``), so the credential denylist, the egress proxy vars and
    the package caches hold there too.

    ``write_root`` is the tree a "workspace" shell may write in, not the
    folder it starts in: a shell started outside the workspace still writes
    only inside it. None writes no tree, only the scratch folder and temp.
    ``secret_reads`` lets the shell read the credential stores (never write).

    Returns ``(argv, profile_path)``. The caller MUST ``os.unlink(profile_path)``
    once the process exits (the profile file must outlive the shell, unlike the
    one-shot run_sandboxed which deletes it immediately).

    Raises SandboxUnavailableError, and wraps nothing, when sandbox-exec
    cannot be used: the shell never starts without the sandbox. A Bypass
    turn's shell is the exception: it gets ``shell_cmd`` as is and no profile.
    """
    if bypass_sandbox.active():
        return list(shell_cmd), None
    _require_sandbox_exec()
    profile = _generate_macos_profile(
        write_root, write_mode=write_mode, scratch_dir=scratch_dir, secret_reads=secret_reads
    )
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="whisper_pty_sandbox_")
    with os.fdopen(fd, "w") as f:
        f.write(profile)
    return ["sandbox-exec", "-f", profile_path, *shell_cmd], profile_path


# ---------------------------------------------------------------------------
# Enforcement facts — who is actually confining, and did the sandbox say no?
# ---------------------------------------------------------------------------


def sandbox_backend() -> str:
    """Which OS backend `run_sandboxed` would use right now: "sandbox-exec",
    or "none" when it cannot be used. With "none" every sandboxed entry point
    raises SandboxUnavailableError, so nothing runs; a caller may check this
    first to refuse with the same message (SANDBOX_UNAVAILABLE_MESSAGE)."""
    return "sandbox-exec" if _is_sandbox_exec_available() else "none"


def pty_sandbox_backend() -> str:
    """Which backend `build_pty_sandbox_wrap` would use: "sandbox-exec" or
    "none", as for `sandbox_backend`, or "bypass" under a Bypass turn's
    scope, where the shell runs with no sandbox and needs none."""
    if bypass_sandbox.active():
        return "bypass"
    return "sandbox-exec" if _is_sandbox_exec_available() else "none"


# The stderr text of "the sandbox blocked a write", per backend. With no
# backend ("none") nothing ran, so nothing is classified.
_DENIAL_SIGNATURES = {
    "sandbox-exec": ("Operation not permitted",),
}


# A shell or sandbox-exec saying it could not start a program: "/bin/sh:
# /bin/ps: Operation not permitted", "zsh:1: operation not permitted: ps",
# "sandbox-exec: execvp() of '/bin/ps' failed: Operation not permitted".
_EXEC_DENIED_RE = re.compile(
    r"^(?:[^\s:]+:(?: ?line \d+:| ?\d+:)? )(/[^\s:]+): Operation not permitted$"
    r"|operation not permitted: (\S+)$"
    r"|execvp\(\) of '([^']+)' failed: Operation not permitted",
    re.MULTILINE | re.IGNORECASE,
)


def _setuid_programs(output: str) -> tuple[list[str], str]:
    """The setuid or setgid programs a confined command could not start, and
    the output without the lines that say so. macOS never lets a sandboxed
    process exec one (the kernel refuses it, whatever the profile allows), so
    no permission or escalation inside the sandbox changes this: /bin/ps and
    /usr/bin/top are the usual ones."""
    found: list[str] = []
    rest = output
    for m in _EXEC_DENIED_RE.finditer(output):
        name = next(g for g in m.groups() if g)
        path = name if os.path.isabs(name) else shutil.which(name) or ""
        try:
            mode = os.stat(path).st_mode if path else 0
        except OSError:
            continue
        if mode & (stat.S_ISUID | stat.S_ISGID):
            rest = rest.replace(m.group(0), "", 1)
            if path not in found:
                found.append(path)
    return found, rest


def _setuid_hint(programs: list[str]) -> str:
    names = ", ".join(programs)
    return (
        f"[sandbox] {names} could not start: macOS never lets a sandboxed command "
        "start a setuid program, whatever the permission mode, so retrying or "
        "escalating does not help. To find processes use pgrep -fl <pattern> "
        "(it runs here); for a background command use task_status or task_output."
    )


def classify_sandbox_denial(output: str, write_mode: str) -> str | None:
    """Best-effort classification: does a failed confined command's output look
    like the OS sandbox denying a write? Returns a model-facing hint naming the
    effective mode and the escalation path, or None.

    A setuid program the sandbox could not start (``ps``) is no write denial:
    it gets its own hint, which names the tool that works instead.

    Only meaningful for confined modes — under "open" the same strings are
    ordinary filesystem errors, so the caller must not pass those here."""
    if write_mode not in ("workspace", "readonly") or not output:
        return None
    signatures = _DENIAL_SIGNATURES.get(sandbox_backend(), ())
    if not any(sig in output for sig in signatures):
        return None
    setuid, rest = _setuid_programs(output) if sandbox_backend() == "sandbox-exec" else ([], "")
    if setuid and not any(sig in rest for sig in signatures):
        return _setuid_hint(setuid)
    mode_label = (
        "read-only (no writes anywhere)"
        if write_mode == "readonly"
        else (
            "workspace-write (writes confined to the working directory, this "
            "session's scratch folder, temp, and /dev)"
        )
    )
    write_hint = (
        f"[sandbox] This command ran under the {mode_label} OS sandbox, and the "
        "failure above looks like a sandbox write denial. If the blocked write is "
        "genuinely needed, retry the same command through terminal_run with "
        "sandbox_permissions='danger-full-access' and a one-line justification; "
        "the user will be asked to approve that exact command before it runs "
        "unconfined."
    )
    return f"{_setuid_hint(setuid)}\n{write_hint}" if setuid else write_hint
