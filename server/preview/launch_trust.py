"""Which ``.whisper/launch.json`` commands a person has approved, per workspace.

A named launch config starts without asking in every permission mode, but only
once a person has seen and approved the command it runs. The file itself proves
nothing about who wrote it: it can arrive with a cloned repo or a ``git pull``,
or be written by an edit that a permissive mode (Accept edits, an Auto
classifier allow, a session "Yes for all") applied with no card. So the
exemption is tied to content a human approved, the way project hooks are
(``server/hooks/config_loader.py``): this store keeps the SHA-256 of every
approved argv under the workspace's real path. A changed command hashes
differently and asks again; deleting the file makes every config ask once more.

Only a person's answer to an approval card records trust (see
``server.approval.spec.execute_approved_by_human``); a start that a mode, a
session approval, the classifier or an unattended agent let through never does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

# Oldest hashes drop first once a workspace holds this many.
MAX_TRUSTED_PER_WORKSPACE = 256

_lock = threading.Lock()


def trust_path() -> str:
    # Resolved per call: data_root() honors WHISPER_DATA_DIR/config at runtime.
    return os.path.join(data_root(), "launch_trust.json")


def command_sha256(argv: list[str]) -> str:
    blob = json.dumps([str(a) for a in argv], separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _load() -> dict[str, list[str]]:
    try:
        with open(trust_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        ws: [h for h in hashes if isinstance(h, str)]
        for ws, hashes in data.items()
        if isinstance(ws, str) and isinstance(hashes, list)
    }


def _key(workspace: str) -> str:
    return os.path.realpath(workspace)


def is_trusted(workspace: str, argv: list[str]) -> bool:
    return command_sha256(argv) in _load().get(_key(workspace), [])


def trust(workspace: str, argv: list[str]) -> None:
    """Record that a person approved ``argv`` as a launch command for ``workspace``."""
    digest = command_sha256(argv)
    key = _key(workspace)
    with _lock:
        data = _load()
        hashes = [h for h in data.get(key, []) if h != digest]
        hashes.append(digest)
        data[key] = hashes[-MAX_TRUSTED_PER_WORKSPACE:]
        path = trust_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    log.info("launch config trusted for %s: %s", key, " ".join(argv)[:200])
