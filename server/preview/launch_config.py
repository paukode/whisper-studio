"""``.whisper/launch.json`` — named dev-server launch configs, per workspace.

Same shape as Claude Code's own ``.claude/launch.json``. Optional convenience:
``preview_start`` can also be called with an ad-hoc command/cwd/port with no
config file present at all.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("whisper-studio")


def _launch_config_path() -> str | None:
    from server.workspace.state import get_workspace_path

    ws = get_workspace_path()
    if not ws:
        return None
    return os.path.join(ws, ".whisper", "launch.json")


def load_launch_configs() -> dict[str, dict]:
    """{name: {runtimeExecutable, runtimeArgs, port}}. Fails soft — a missing
    or malformed file just means no named configs are available."""
    path = _launch_config_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, errors="replace") as f:
            data = json.load(f)
        configs = data.get("configurations", [])
        return {c["name"]: c for c in configs if isinstance(c, dict) and c.get("name")}
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to load .whisper/launch.json: %s", e)
        return {}


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
