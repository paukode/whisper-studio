"""Hook config: user layer, project layer, and the project-trust store.

User layer: data_root()/hooks.json (v2, or legacy v1 normalized on load).
Project layer: the "hooks" key in <workspace>/.whisper/settings.json — arbitrary
code from a cloned repo, so INERT until the workspace's project-hook set is
explicitly trusted (SHA-256 of the canonical hooks, mirroring trusted skills).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

from server.hooks.schema import HookDef, normalize_config, serialize_v2
from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")


def hooks_path() -> str:
    # Resolved per call — data_root() honors WHISPER_DATA_DIR/config at runtime,
    # so a module-level constant would freeze the path (and break test isolation).
    return os.path.join(data_root(), "hooks.json")


def trust_path() -> str:
    return os.path.join(data_root(), "hooks_trust.json")


def _load_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_user_hooks() -> dict[str, list[HookDef]]:
    return normalize_config(_load_json(hooks_path()), source="user")


def save_user_hooks(by_event: dict[str, list[HookDef]]) -> None:
    path = hooks_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(serialize_v2(by_event), f, indent=2)
    os.replace(tmp, path)


def upsert_user_hook(hook: HookDef) -> HookDef:
    """Add a new user hook (blank id) or replace an existing one by id.
    Returns the stored HookDef (with a generated id when new)."""
    hook = hook.clamp()
    by_event = load_user_hooks()
    for ev in by_event:
        by_event[ev] = [h for h in by_event[ev] if h.id != hook.id]
    by_event.setdefault(hook.event, []).append(hook)
    save_user_hooks(by_event)
    return hook


def delete_user_hook(hook_id: str) -> bool:
    by_event = load_user_hooks()
    removed = False
    for ev in by_event:
        before = len(by_event[ev])
        by_event[ev] = [h for h in by_event[ev] if h.id != hook_id]
        removed = removed or len(by_event[ev]) != before
    if removed:
        save_user_hooks(by_event)
    return removed


def _project_hooks_raw(workspace: str | None) -> dict | None:
    if not workspace:
        return None
    settings = _load_json(os.path.join(workspace, ".whisper", "settings.json"))
    if isinstance(settings, dict) and settings.get("hooks"):
        return {"hooks": settings["hooks"]}
    return None


def _canonical_hash(raw: dict) -> str:
    blob = json.dumps(raw.get("hooks", {}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _load_trust() -> dict:
    data = _load_json(trust_path())
    return data if isinstance(data, dict) else {}


def _real(workspace: str) -> str:
    return os.path.realpath(workspace)


def _raw_is_trusted(workspace: str | None, raw: dict) -> bool:
    """Is THIS exact hook document trusted? Callers must pass the same ``raw``
    they will execute, so the validated bytes and the run bytes are identical
    (no TOCTOU from a second independent file read)."""
    trusted = _load_trust().get(_real(workspace or ""))
    return trusted == _canonical_hash(raw)


def project_trust_status(workspace: str | None) -> str:
    """ "none" (no project hooks), "trusted", or "pending_approval"."""
    raw = _project_hooks_raw(workspace)
    if not raw:
        return "none"
    return "trusted" if _raw_is_trusted(workspace, raw) else "pending_approval"


def approve_project_hooks(workspace: str) -> bool:
    raw = _project_hooks_raw(workspace)
    if not raw:
        return False
    trust = _load_trust()
    trust[_real(workspace)] = _canonical_hash(raw)
    path = trust_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trust, f, indent=2)
    os.replace(tmp, path)
    return True


def revoke_project_hooks(workspace: str) -> bool:
    """Drop trust for a workspace's project hooks — they go inert again."""
    trust = _load_trust()
    if _real(workspace) not in trust:
        return False
    del trust[_real(workspace)]
    path = trust_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trust, f, indent=2)
    os.replace(tmp, path)
    return True


def load_project_hooks(workspace: str | None) -> dict[str, list[HookDef]]:
    """Project hooks ONLY when trusted; otherwise empty (they stay inert).

    The trust check hashes the SAME ``raw`` that gets normalized and executed —
    a single read closes the TOCTOU where a concurrent writer could swap in
    malicious hooks between the trust check and the load."""
    raw = _project_hooks_raw(workspace)
    if not raw or not _raw_is_trusted(workspace, raw):
        return normalize_config(None, source="project")
    return normalize_config(raw, source="project")


def merged_for_event(event: str, workspace: str | None) -> list[HookDef]:
    """Enabled user + trusted-project shell hooks for an event, user first."""
    user = load_user_hooks().get(event, [])
    project = load_project_hooks(workspace).get(event, [])
    return [d for d in [*user, *project] if d.enabled]
