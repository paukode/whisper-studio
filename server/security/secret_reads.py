"""Reading the credential stores: the Keychain, ~/.ssh, ~/.aws and the rest
of server/security/sensitive_paths.py.

The user's table, per permission mode:

- bypassPermissions: every read tool and every command reads them with no
  card. The OS sandbox still never lets a command WRITE them.
- dontAsk: refused.
- every other mode (default, acceptEdits, plan, auto): a person approves each
  read. A file read of a secret path becomes a ``secret_read`` approval card,
  and a command asks with ``secret_access=true`` (its card says so). The Auto
  classifier never decides it, and a blanket "Yes, all" never covers it.
- an unattended run (an agent, cron) outside bypassPermissions has no person
  to ask, so it is refused.

``tool_executor`` stamps SECRET_READS_KEY on every call (True only in
bypassPermissions, overwriting anything the model sent), so a read tool knows
the mode without importing the turn. The approval gate
(server/tool_approvals.py) is the one place that decides a command's
``secret_access``: it sets it in bypassPermissions and otherwise lets it reach
an executor only through a person's yes.
"""

from __future__ import annotations

from server.security.permissions import MODE_BYPASS, MODE_DONT_ASK

# Stamped by server/tool_executor.py on every call input.
SECRET_READS_KEY = "__secret_reads__"

# The command-tool input that asks to read the credential stores.
SECRET_ACCESS_KEY = "secret_access"

# The approval action and category of a file read of a secret path.
SECRET_READ_ACTION = "secret_read"
SECRET_READ_CATEGORY = "secret-read"

REFUSED_DONT_ASK = (
    "[Refused] Reading the credential stores (Keychain, ~/.ssh, ~/.aws and "
    "similar) is not allowed in Don't ask mode. Ask the user to switch the "
    "permission mode if this read is needed."
)
REFUSED_UNATTENDED = (
    "[Refused] Reading the credential stores (Keychain, ~/.ssh, ~/.aws and "
    "similar) needs a person's approval, and this run has no person to ask. "
    "Ask the top-level session to do this read."
)


def decision(mode: str) -> str:
    """ "allow" | "ask" | "deny" for a secret read in ``mode``."""
    if mode == MODE_BYPASS:
        return "allow"
    if mode == MODE_DONT_ASK:
        return "deny"
    return "ask"


def stamped(call_input: dict | None) -> bool:
    """Whether the call runs in bypassPermissions (tool_executor's stamp)."""
    return bool((call_input or {}).get(SECRET_READS_KEY))


def requested(tool_input: dict | None) -> bool:
    """Whether a command call asks to read the credential stores."""
    value = (tool_input or {}).get(SECRET_ACCESS_KEY)
    return value is True or str(value).strip().lower() == "true"


# What a command prints when the sandbox kept it from a credential store.
_KEYCHAIN_SIGNATURES = (
    "A default keychain could not be found",
    "The specified keychain could not be found",
    "SecKeychain",
)


def denial_hint(command: str, output: str) -> str | None:
    """A model-facing hint when a command that ran WITHOUT secret reads
    failed in a way that looks like the sandbox hid a credential store:
    a Keychain error, or "Operation not permitted" on a deny-listed path the
    command or its output names. None otherwise."""
    if not output:
        return None
    from server.security.sensitive_paths import expanded_sandbox_paths

    keychain = any(sig in output for sig in _KEYCHAIN_SIGNATURES)
    denied = "Operation not permitted" in output and any(
        path in output or path in (command or "") for path in expanded_sandbox_paths()
    )
    if not (keychain or denied):
        return None
    return (
        "[sandbox] The OS sandbox hides the credential stores (Keychain, ~/.ssh, "
        "~/.aws and similar) from this command. If the command needs them, run it "
        "again with secret_access=true: the user is asked to approve that exact "
        "command first."
    )


# ── File reads of a secret path (ws_read_file, ws_list_directory) ─────────

# The read tools a secret_read card can run, and the inputs it keeps.
_READ_TOOLS = ("ws_read_file", "ws_list_directory")
_READ_FIELDS = ("path", "offset", "limit")


def read_request(tool_name: str, tool_input: dict, session_id: str) -> str:
    """The approval request a read tool returns for a secret path outside
    bypassPermissions: the gate asks a person, or refuses (see decision)."""
    import json

    from server.approval.markers import approval_request

    payload = {"action": SECRET_READ_ACTION, "tool": tool_name, "session_id": session_id}
    payload.update({k: tool_input[k] for k in _READ_FIELDS if k in tool_input})
    return approval_request(json.dumps(payload))


async def _do_secret_read(payload: dict):
    """Run the read a person approved, with the secret path readable."""
    import asyncio

    from server.approval.spec import ApprovalOutcome, refuse_if_agent

    refusal = refuse_if_agent(payload, what="Reading a credential store")
    if refusal:
        return refusal
    tool = payload.get("tool")
    if tool not in _READ_TOOLS:
        return ApprovalOutcome(ok=False, error=f"Not a read tool: {tool!r}")
    import server.workspace  # noqa: F401 (registers the read tools)
    from server.executors import EXECUTORS

    tool_input = {k: payload[k] for k in _READ_FIELDS if k in payload}
    tool_input["__session_id__"] = payload.get("session_id") or ""
    tool_input[SECRET_READS_KEY] = True
    output = await asyncio.to_thread(EXECUTORS[tool], tool_input, "", {})
    return ApprovalOutcome(ok=True, output=output)


def _read_summary(payload: dict) -> str:
    what = "List" if payload.get("tool") == "ws_list_directory" else "Read"
    return (
        f"{what} {payload.get('path') or '?'}, a credential store (Keychain, keys, "
        "tokens or browser data)"
    )


def register_secret_read_approval() -> None:
    """The ApprovalSpec for a read of a secret path, called from
    server/approval/bootstrap.py's register_defaults()."""
    from server.approval.registry import register
    from server.approval.spec import ApprovalSpec

    register(
        SECRET_READ_ACTION,
        ApprovalSpec(
            category=SECRET_READ_CATEGORY,
            preview="text",
            summary=_read_summary,
            executor=_do_secret_read,
            risk_hint="high",
            payload_fields=["tool", *_READ_FIELDS, "session_id"],
        ),
    )
