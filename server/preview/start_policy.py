"""When ``preview_start`` needs no approval: an approved ``.whisper/launch.json`` config.

The user's rule: whatever permission mode is selected, a preview should be
allowed to start. It covers a call that names a launch config, so the command
comes wholly from the workspace's launch.json, and only a command a person has
approved once (``server/preview/launch_trust.py``): the file can arrive with a
cloned repo or be written by an edit a permissive mode applied with no card, so
its presence proves nothing. The first start of a new or changed command
follows the mode; approving that card trusts the command, and from then on it
starts in every mode. An ad-hoc call (``runtimeExecutable``/``runtimeArgs`` in
the input) follows the permission mode exactly like any other command.

What runs is what was decided and shown. ``pin_launch_config`` freezes the
resolved config into the call's payload when the tool is dispatched
(``server/preview/router.py``); the approval card shows that pinned command,
``server.preview.manager.start_preview_session`` runs it, and the exemption
holds only while launch.json still resolves to it.

Every place that decides a preview_start reads these helpers, so the rule lives
here once:

  - ``server.security.permissions.approval_floor`` checks the hard floors
    against the command the config resolves to and the pinned one
    (``floor_commands``), so a launch.json entry that runs an rm-class command,
    also inside ``sh -c``, still asks.
  - ``server.security.permissions.resolve_static_decision`` lets a trusted
    named config stand in for the mode's own default (``starts_without_approval``),
    after the floors, bypass, session approvals and custom rules. The global
    mode, a per-category mode for ``preview``, plan mode, dontAsk and auto (no
    classifier call) all yield to it; outside bypass, an explicit deny rule or
    a session "No for all" on the preview category still refuses it.
  - ``server.tool_executor.execute_tool_batch`` does not hold a call back on
    the per-tool denial counter when it will start with no card at all
    (``never_prompts``).
  - ``server.preview.router`` pins the config into the payload
    (``pin_launch_config``); ``server.approval.bootstrap`` carries the pin to
    the card and the executor, renders the card, and records trust when a
    person approves it (``trust_approved``).
"""

from __future__ import annotations

import os
import shlex

PREVIEW_START = "preview_start"
PREVIEW_CATEGORY = "preview"
# The payload key the resolved config is frozen under.
PIN_KEY = "launch_config"


def _config_name(tool_name: str, tool_input: dict) -> str | None:
    """The launch config a preview_start names, or None for any other tool and
    for an ad-hoc call (a truthy ``runtimeExecutable``, which is exactly the
    branch ``start_preview_session`` takes)."""
    if tool_name != PREVIEW_START or tool_input.get("runtimeExecutable"):
        return None
    name = tool_input.get("session_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def resolve_named(tool_name: str, tool_input: dict) -> dict | None:
    """``{"command", "port", "workspace"}`` as .whisper/launch.json has it now.

    None when the call is not a named preview_start, no workspace is connected,
    or the name does not resolve (that call fails at execution).
    """
    name = _config_name(tool_name, tool_input)
    if name is None:
        return None
    from server.preview.launch_config import resolve_launch_command
    from server.workspace.state import get_workspace_path

    workspace = get_workspace_path()
    if not workspace:
        return None
    resolved = resolve_launch_command(name)
    if not resolved:
        return None
    return {**resolved, "workspace": os.path.realpath(workspace)}


def pinned_config(tool_input: dict) -> dict | None:
    """The config frozen into this payload by ``pin_launch_config``, or None when
    it carries none (or a malformed one)."""
    pin = tool_input.get(PIN_KEY)
    if not isinstance(pin, dict):
        return None
    argv = pin.get("command")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return None
    port = pin.get("port")
    workspace = pin.get("workspace")
    return {
        "command": list(argv),
        "port": port if isinstance(port, int) and not isinstance(port, bool) else None,
        "workspace": workspace if isinstance(workspace, str) and workspace else None,
    }


def pin_launch_config(payload: dict) -> None:
    """Freeze the config a named preview_start resolves to into its payload.

    Called where the tool is dispatched, after the model's own input is copied
    in, so a model-supplied value never survives.
    """
    payload.pop(PIN_KEY, None)
    resolved = resolve_named(PREVIEW_START, payload)
    if resolved is not None:
        payload[PIN_KEY] = resolved


def display_command(tool_input: dict) -> str | None:
    """The pinned command, quoted the way a shell would read it, for the card."""
    if _config_name(PREVIEW_START, tool_input) is None:
        return None
    pin = pinned_config(tool_input)
    return shlex.join(pin["command"]) if pin else None


def floor_commands(tool_name: str, tool_input: dict) -> list[str]:
    """The command lines the hard floors must see for a named preview_start.

    Both the config launch.json resolves to now and the pinned one, since the
    pin is what runs. Each argv is joined with plain spaces, not shell quoting,
    the way ``DevServerProcess.spawn`` validates it (the process is exec'd
    without a shell, and quoting would hide an argument such as
    ``"build && rm -rf dist"`` from the checks); any script it hands a shell
    with -c is added as a command line of its own, so
    ``sh -c "rm -rf build && npm run dev"`` does not read as the command ``sh``.
    """
    if _config_name(tool_name, tool_input) is None:
        return []
    from server.security.command_validator import shell_scripts

    argvs: list[list[str]] = []
    for config in (resolve_named(tool_name, tool_input), pinned_config(tool_input)):
        if config is not None and config["command"] not in argvs:
            argvs.append(config["command"])
    return [line for argv in argvs for line in (" ".join(argv), *shell_scripts(argv))]


def _cwd_inside(cwd: object, workspace: str) -> bool:
    """A ``cwd`` in the input is part of the command's reach, so it keeps the
    exemption only while it resolves inside the workspace the config came from
    (the default when omitted). It is resolved the way the spawn will use it:
    no ``~`` expansion, and a relative path against the server's own working
    directory."""
    if not cwd:
        return True
    if not isinstance(cwd, str):
        return False
    try:
        real_cwd = os.path.realpath(cwd)
    except (OSError, ValueError):
        return False
    return real_cwd == workspace or real_cwd.startswith(workspace.rstrip(os.sep) + os.sep)


def _exempt_target(tool_name: str, tool_input: dict) -> dict | None:
    """The config this call starts when everything but trust qualifies it: a
    named config pinned when the call was dispatched, that launch.json still
    resolves to, run inside its workspace. A call with no pin (the name did not
    resolve when the model made it) does not qualify."""
    pin = pinned_config(tool_input)
    if pin is None:
        return None
    current = resolve_named(tool_name, tool_input)
    if current is None or (pin["command"], pin["workspace"]) != (
        current["command"],
        current["workspace"],
    ):
        return None  # launch.json changed since the call was made
    if not _cwd_inside(tool_input.get("cwd"), current["workspace"]):
        return None
    return current


def starts_without_approval(tool_name: str, tool_input: dict) -> bool:
    """True when this call is a named preview_start of a command a person approved."""
    target = _exempt_target(tool_name, tool_input)
    if target is None:
        return False
    from server.preview.launch_trust import is_trusted

    return is_trusted(target["workspace"], target["command"])


def _nothing_else_asks(tool_name: str, tool_input: dict) -> bool:
    from server.security.permissions import approval_floor, evaluate_rules

    if approval_floor(tool_name, tool_input, PREVIEW_CATEGORY) is not None:
        return False
    return evaluate_rules(tool_name, tool_input) in (None, "allow")


def never_prompts(tool_name: str, tool_input: dict) -> bool:
    """True when this call, as the model made it, starts with no card in any
    mode: trusted, no hard floor, and no rule of the user's that asks or
    denies. The per-tool denial counter counts refused cards, so it must not
    hold back a call that never shows one, and must keep holding back one that
    does. It runs before dispatch, so the call is pinned here the way the
    preview router will pin it."""
    if _config_name(tool_name, tool_input) is None:
        return False
    call = dict(tool_input)
    pin_launch_config(call)
    return starts_without_approval(tool_name, call) and _nothing_else_asks(tool_name, call)


def approval_trusts_it(tool_input: dict) -> bool:
    """For the card: approving it is what lets later starts skip the card (the
    command is new or changed, and nothing else would keep asking)."""
    target = _exempt_target(PREVIEW_START, tool_input)
    if target is None:
        return False
    from server.preview.launch_trust import is_trusted

    if is_trusted(target["workspace"], target["command"]):
        return False
    return _nothing_else_asks(PREVIEW_START, tool_input)


def trust_approved(payload: dict) -> None:
    """A person approved this preview_start card: trust the pinned command.

    The pin is what the card showed and what runs, so it is what gets trusted,
    for the workspace it was resolved from. An ad-hoc call, or a payload with
    no pin, records nothing.
    """
    if _config_name(PREVIEW_START, payload) is None:
        return
    pin = pinned_config(payload)
    if pin is None or not pin["workspace"]:
        return
    from server.preview.launch_trust import trust

    trust(pin["workspace"], pin["command"])
