import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile

import boto3

from server.executors import register_executor
from server.sandbox import run_sandboxed
from server.security.command_validator import validate_command

log = logging.getLogger("whisper-studio")

# Cloud-credential dirs the AWS tools legitimately need — re-allowed through the
# sandbox deny-list so aws_cli / boto3 can authenticate, while ~/.ssh, git
# credentials, /etc/shadow, etc. stay blocked.
_CLOUD_CRED_PATHS = [os.path.expanduser("~/.aws")]

# Read-only AWS guardrail.
#
# DEFENSE IN DEPTH ONLY — NOT A SECURITY BOUNDARY.
#
# This string is prepended to user/model Python code at execution time
# and runtime-monkey-patches `botocore.endpoint.Endpoint.make_request`
# to reject any non-list/get/describe AWS operation. A motivated
# attacker (or model) can defeat it in trivial ways inside the same
# process:
#   - `import botocore.endpoint as _ep; del _ep.Endpoint.make_request`
#   - `_ep.Endpoint.make_request = _orig_make_request`
#   - `subprocess.run(["python3", "-c", "import boto3; ..."])` to spawn
#     a child without the patch.
#
# The REAL boundary is `sandbox-exec` wrapping the entire subprocess
# (see do_run_python below + server/sandbox.py). That's macOS-level
# kernel enforcement of filesystem + network policy — that's what
# actually contains malicious code.
#
# Keep this guard for what it's good at: catching accidental write
# calls from a model that thought it was using boto3 in read-only mode
# and would otherwise burn through write rate-limits or alarm a
# monitoring tool. Don't treat its presence as making "run any AWS
# Python" safe.
# Single source of truth for "read-shaped" AWS operations, shared by the
# in-process boto3 guard (run_python) and the aws_boto3 executor's allowlist.
# Allowlist > denylist: read-shaped-but-mutating ops (assume_role, accept_*,
# complete_*, submit_*, …) are not "writes" by prefix yet still mutate, so a
# write-prefix denylist leaks them.
_AWS_READ_PREFIXES = (
    "list",
    "get",
    "describe",
    "head",
    "query",
    "search",
    "lookup",
    "batch_get",
    "scan",
    "select",
)

# Wall-clock budget for approved Python. Generous enough to read a real
# spreadsheet end to end (pandas opens a 3MB / 10k-row xlsx in about two
# seconds, and attachments run to 50MB), short enough that a runaway loop
# still ends on its own. The user has already approved the code by the time
# it runs, and it runs under the OS sandbox either way.
RUN_PYTHON_TIMEOUT_S = 90

_AWS_READONLY_GUARD = """
import botocore.endpoint as _ep
_orig_make_request = _ep.Endpoint.make_request
_AWS_READ_PREFIXES = __READ_PREFIXES__
def _guarded_make_request(self, operation_model, request_dict):
    op = operation_model.name
    op_snake = "".join("_" + c.lower() if c.isupper() else c for c in op).lstrip("_")
    if not op_snake.startswith(_AWS_READ_PREFIXES):
        raise PermissionError(f"BLOCKED: AWS write operation '{op}' is not allowed. Only read operations (list/get/describe) are permitted.")
    return _orig_make_request(self, operation_model, request_dict)
_ep.Endpoint.make_request = _guarded_make_request
""".replace("__READ_PREFIXES__", repr(_AWS_READ_PREFIXES))


def _python_write_mode(payload: dict) -> tuple[str, str]:
    """Map run_python's (sandbox_permissions, justification) to a write mode.

    Returns (write_mode, error). Default is workspace confinement (the harness
    decides; the model doesn't have to be explicit): writes limited to the
    workspace, temp, and ~/.aws. danger-full-access lifts that for ONE approved
    call — it requires a justification, always forces a real approval prompt
    (see resolve_static_decision), and is refused from unattended agents."""
    perms = (payload.get("sandbox_permissions") or "workspace-write").strip()
    if perms in ("workspace-write", ""):
        return "workspace", ""
    if perms == "read-only":
        return "readonly", ""
    if perms != "danger-full-access":
        return "", f"invalid sandbox_permissions {perms!r}"
    if payload.get("__agent__"):
        return "", (
            "danger-full-access is not permitted from an unattended subagent. "
            "Ask the top-level session, where a human can approve it, to run this."
        )
    if not (payload.get("justification") or "").strip():
        return "", (
            "sandbox_permissions='danger-full-access' requires a justification "
            "explaining why the script must write outside the workspace."
        )
    return "open", ""


def _interpreter() -> str:
    """The Python this server runs on, or plain python3 when unknown."""
    exe = sys.executable or ""
    return exe if exe and os.path.isfile(exe) else "python3"


def do_run_python(payload: dict) -> tuple[bool, str]:
    """Execute previously approved Python under the OS sandbox (the real
    boundary — see module docstring). Returns (ok, output_or_error)."""
    code = payload.get("code", "")
    if not code:
        return False, "no code provided"
    write_mode, perm_error = _python_write_mode(payload)
    if perm_error:
        return False, perm_error
    guarded_code = _AWS_READONLY_GUARD + "\n" + code
    # run_sandboxed runs `/bin/sh -c <str>`, so the code goes to a temp file and
    # we invoke it by path. ~/.aws is allowed so boto3 can authenticate. The
    # cwd is the workspace when one is connected so workspace-write confinement
    # covers the project tree the script most plausibly writes to.
    from server.workspace import get_workspace_path

    cwd = get_workspace_path() or "/tmp"
    if not os.path.isdir(cwd):
        cwd = "/tmp"
    fd, script_path = tempfile.mkstemp(suffix=".py", prefix="whisper_run_", dir="/tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(guarded_code)
        # The app's own interpreter (the bundled Python in the Mac app, the
        # venv in dev), never the machine's python3: it is the one that has
        # pandas, openpyxl, python-docx, python-pptx and friends installed.
        result = run_sandboxed(
            f"{shlex.quote(_interpreter())} {shlex.quote(script_path)} < /dev/null",
            cwd=cwd,
            timeout=RUN_PYTHON_TIMEOUT_S,
            allow_paths=_CLOUD_CRED_PATHS,
            write_mode=write_mode,
        )
    except subprocess.TimeoutExpired:
        return False, f"execution timed out ({RUN_PYTHON_TIMEOUT_S}s limit)"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass
    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    output = output.strip() or "(no output)"
    # Cap like the AWS executors (aws_boto3 100k, aws_cli 10k) so a runaway
    # print loop can't flood the model's context with unbounded output.
    if len(output) > 100_000:
        output = output[:100_000] + "\n... (truncated)"
    if result.returncode not in (0, None) and write_mode in ("workspace", "readonly"):
        from server.sandbox import classify_sandbox_denial

        hint = classify_sandbox_denial(output, write_mode)
        if hint:
            output = f"{output}\n{hint}"
    return True, output


@register_executor("run_python", read_only=False, concurrent_safe=False)
def exec_run_python(tool_input, transcript, current_attachments):
    """Emit an approval request. The real Python runs only after the user
    clicks Yes (or 'Yes, all commands' is set for the session)."""
    code = tool_input.get("code", "")
    if not code:
        return "Error: no code provided."
    payload = json.dumps(
        {
            "action": "run_python",
            "code": code,
            "sandbox_permissions": (tool_input.get("sandbox_permissions") or "").strip(),
            "justification": (tool_input.get("justification") or "").strip(),
        }
    )
    return f"[WS_APPROVAL]{payload}"


@register_executor("aws_boto3", read_only=True, concurrent_safe=True)
def exec_aws_boto3(tool_input, transcript, current_attachments):
    service = tool_input.get("service", "").strip()
    method = tool_input.get("method", "").strip()
    params = tool_input.get("params", {})
    region = tool_input.get("region", "us-east-1").strip()
    if not service or not method:
        return "Missing required 'service' and 'method' parameters."
    # Allowlist (shared with the run_python boto3 guard): only read-shaped
    # methods are permitted. boto3 client methods are snake_case, so a prefix
    # match against _AWS_READ_PREFIXES is exact.
    method_lower = method.lower()
    if not method_lower.startswith(_AWS_READ_PREFIXES):
        return (
            f"Blocked: '{method}' is not a read-only operation. Only read methods "
            "(list/get/describe/head/query/scan/select/lookup/search) are allowed here. "
            "Use the aws_cli tool for writes — it requires user approval."
        )
    try:
        client = boto3.client(service, region_name=region)
        fn = getattr(client, method, None)
        if fn is None:
            return f"Method '{method}' not found on boto3 client '{service}'."
        if isinstance(params, str):
            params = json.loads(params)
        result = fn(**params)
        result.pop("ResponseMetadata", None)
        output = json.dumps(result, indent=2, default=str)
        if len(output) > 100000:
            output = output[:100000] + "\n... (truncated)"
        return output
    except Exception as e:
        return f"Error: {e}"


def do_aws_cli(payload: dict) -> tuple[bool, str]:
    """Execute a previously approved aws CLI command. Returns (ok, output_or_error)."""
    command = (payload.get("command") or "").strip()
    if not command:
        return False, "no command provided"
    if not command.startswith("aws "):
        return False, "invalid command — must start with 'aws'"
    warning = validate_command(command)
    if warning:
        return False, warning
    from server.workspace import get_workspace_path

    cwd = get_workspace_path() or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    try:
        result = run_sandboxed(
            f"{command} < /dev/null",
            cwd=cwd,
            timeout=30,
            allow_paths=_CLOUD_CRED_PATHS,
            # Always workspace-write confined: the CLI's only legitimate local
            # writes are its own ~/.aws cache (kept writable via allow_paths),
            # the workspace (s3 cp downloads), and temp. Remote AWS mutations
            # are unaffected — this confines the LOCAL filesystem only.
            write_mode="workspace",
        )
    except subprocess.TimeoutExpired:
        return False, "command timed out after 30s"
    except Exception as e:
        return False, str(e)
    output = (result.stdout + result.stderr).strip()
    if len(output) > 10_000:
        output = output[:10_000] + "\n... (truncated)"
    if result.returncode == 0:
        return True, output or "(command completed successfully, no output)"
    return (
        False,
        f"(exit code {result.returncode})\n{output}"
        if output
        else f"(exit code {result.returncode})",
    )


@register_executor("aws_cli", read_only=False, concurrent_safe=False)
def exec_aws_cli(tool_input, transcript, current_attachments):
    """Emit an approval request. The actual aws command runs only after
    the user clicks Yes (or 'Yes, all commands' is set for the session).
    `check_params` mode is a dry-run flag that bypasses approval since
    it doesn't actually call AWS."""
    command = tool_input.get("command", "").strip()
    mode = tool_input.get("mode", "").strip()
    if not command:
        return "No command provided."
    if mode == "check_params":
        return f"[PARAM_CHECK]\n{command}"
    if not command.startswith("aws "):
        return "Invalid command — must start with 'aws'."
    payload = json.dumps({"action": "aws_cli", "command": command})
    return f"[WS_APPROVAL]{payload}"
