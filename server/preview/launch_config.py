"""``.whisper/launch.json`` — named dev-server launch configs, per workspace.

Same shape as Claude Code's own ``.claude/launch.json``. Optional convenience:
``preview_start`` can also be called with an ad-hoc command/cwd/port with no
config file present at all.
"""

from __future__ import annotations

import json
import os


def _launch_config_path() -> str | None:
    from server.workspace.state import get_workspace_path

    ws = get_workspace_path()
    if not ws:
        return None
    return os.path.join(ws, ".whisper", "launch.json")


def load_launch_configs() -> dict[str, dict]:
    """{name: {runtimeExecutable, runtimeArgs, port}}. A missing file (or no
    workspace) has none; a file that cannot be read or parsed raises
    ValueError naming it and the error."""
    return _load(_launch_config_path())


def _load(path: str | None) -> dict[str, dict]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"Could not read {path}: {e}") from e
    configs = data.get("configurations", []) if isinstance(data, dict) else None
    if not isinstance(configs, list):
        raise ValueError(f"Could not read {path}: 'configurations' must be a list")
    return {c["name"]: c for c in configs if isinstance(c, dict) and c.get("name")}


def resolve_launch_command(name: str) -> dict | None:
    """Returns {"command": [...], "port": int|None} for a named config, or
    None if not found."""
    entry = load_launch_configs().get(name)
    if not entry:
        return None
    exe = entry.get("runtimeExecutable")
    args = entry.get("runtimeArgs", [])
    if not exe:
        return None
    return {"command": [str(exe), *[str(a) for a in args]], "port": entry.get("port")}


def missing_config_message(name: str, cwd: str | None = None) -> str:
    """Why a preview_start that names ``name`` and no command cannot start:
    the file read, the names it has, and what to do. Only the connected
    workspace's file is read, so a project folder's own launch.json inside it
    is not; a field session asked for one six times over two days on the
    bare "no entry named" line."""
    path = _launch_config_path()
    if not path:
        return (
            f"No launch configuration named '{name}': no workspace is connected, so no "
            ".whisper/launch.json is read. Pass runtimeExecutable and runtimeArgs instead."
        )
    names = sorted(_load(path))
    if names:
        have = f"Its configurations: {', '.join(names)}."
    elif os.path.isfile(path):
        have = "It has no configurations."
    else:
        have = "That file does not exist."
    msg = f"No launch configuration named '{name}' in {path}. {have}"
    other = os.path.join(os.path.expanduser(cwd), ".whisper", "launch.json") if cwd else ""
    if not other or os.path.realpath(other) == os.path.realpath(path):
        return f"{msg} Use one of those names, or pass runtimeExecutable and runtimeArgs."
    try:
        in_other = name in _load(other)
    except ValueError as e:
        return f"{msg} {e}"
    if in_other:
        return (
            f"{msg} {other} has '{name}', but only the connected workspace's "
            ".whisper/launch.json is read: pass that entry's runtimeExecutable, "
            "runtimeArgs and port directly, with cwd."
        )
    return f"{msg} Use one of those names, or pass runtimeExecutable and runtimeArgs."
