"""Hook config: user layer, project layer, and the project-trust store.

User layer: data_root()/hooks.json (v2). A file in the old v1 list format is
not read and never overwritten; a notice says so.
Project layer: the "hooks" key in <workspace>/.whisper/settings.json: arbitrary
code from a cloned repo, so INERT until the workspace's project-hook set is
explicitly trusted (SHA-256 of the canonical hooks, mirroring trusted skills).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from server.hooks.schema import HOOK_EVENTS, HookDef, normalize_config, serialize_v2
from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

# Notices for old-format hook config (server.notifications.old_format_notice).
V1_NOTICE_TITLE = "Old hooks.json format is not read"
PROJECT_V1_NOTICE_TITLE = "Old project hooks format is not read"
TURN_END_NOTICE_TITLE = "TurnEnd hooks are not run"
OLD_VARS_NOTICE_TITLE = "Hook not run: old WHISPER_ variable"

# The variables hooks used to get, and the stdin JSON field that replaces each.
OLD_HOOK_VARS = {
    "WHISPER_EVENT": "event",
    "WHISPER_TOOL": "tool_name",
    "WHISPER_INPUT": "tool_input",
    "WHISPER_OUTPUT": "tool_output",
    "WHISPER_SESSION": "session_id",
}
# Any mention counts, not only $NAME: a command can also read them through
# printenv or os.environ. A script file the command runs is not looked into.
_OLD_VAR_RE = re.compile(r"\b(" + "|".join(OLD_HOOK_VARS) + r")\b")


class HooksFileNotSaved(Exception):
    """hooks.json is left as it is: the app does not overwrite it."""


class OldHooksFile(HooksFileNotSaved):
    """hooks.json is in the old v1 format, so the app does not overwrite it."""


class UnreadableHooksFile(HooksFileNotSaved):
    """hooks.json exists but cannot be read or parsed, so the app does not
    overwrite it (that would drop every hook in it)."""


def _notice(title: str, message: str) -> None:
    from server.notifications import old_format_notice

    old_format_notice(title=title, message=message)


def _is_v1(raw) -> bool:
    """The old v1 format: a flat list of {event, tool, command}, bare or under "hooks"."""
    return isinstance(raw, list) or (isinstance(raw, dict) and isinstance(raw.get("hooks"), list))


def _report_turn_end(raw, where: str) -> None:
    """Notice for hooks on the old "TurnEnd" event, which are not registered."""
    hooks = raw.get("hooks", raw) if isinstance(raw, dict) else None
    if isinstance(hooks, dict) and "TurnEnd" in hooks:
        _notice(
            TURN_END_NOTICE_TITLE,
            f'{where} has hooks on the event "TurnEnd", which the app no longer reads, '
            'so they are not run. The file is left as it is. Move them to the "Stop" '
            "event, which runs at the end of each turn.",
        )


def old_hook_vars(command: str) -> list[str]:
    """The old WHISPER_* variables a hook command names, in order: $WHISPER_TOOL,
    ${WHISPER_TOOL}, printenv WHISPER_TOOL, os.environ["WHISPER_TOOL"], ..."""
    return list(dict.fromkeys(_OLD_VAR_RE.findall(command or "")))


def old_vars_message(hook_label: str, names: list[str]) -> str:
    """What a hook that uses old variables should read instead."""
    uses = " and ".join(names)
    fields = " and ".join(OLD_HOOK_VARS[n] for n in names)
    return (
        f"{hook_label} uses {uses}, which hooks no longer get, so it is not run. "
        f"Read {fields} from the event JSON on stdin instead (for example "
        f"jq -r .{OLD_HOOK_VARS[names[0]]}) and change the command."
    )


def hook_label(hook: HookDef) -> str:
    """A stable name for a hook in a notice (a hook without a stored id gets a
    new one on every load, so the command names it)."""
    cmd = hook.command if len(hook.command) <= 80 else hook.command[:77] + "..."
    return f'The {hook.source} {hook.event} hook "{cmd}"'


def hooks_path() -> str:
    # Resolved per call: data_root() honors WHISPER_DATA_DIR/config at runtime,
    # so a module-level constant would freeze the path (and break test isolation).
    return os.path.join(data_root(), "hooks.json")


def trust_path() -> str:
    return os.path.join(data_root(), "hooks_trust.json")


def _read_json(path: str):
    """The parsed file, or None when it does not exist. Raises on a file that
    cannot be read or parsed."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _load_json(path: str):
    """The parsed file, or None when it does not exist or cannot be read (its
    hooks are then not run; the failure is logged)."""
    try:
        return _read_json(path)
    except (OSError, ValueError) as e:
        log.warning("Hooks file %s not read: %s", path, e)
        return None


def load_user_hooks() -> dict[str, list[HookDef]]:
    path = hooks_path()
    raw = _load_json(path)
    if _is_v1(raw):
        _notice(
            V1_NOTICE_TITLE,
            f"{path} is in the old v1 list format, which the app no longer reads, so "
            "none of its hooks run and new hooks cannot be saved. The file is left as "
            "it is. Move or rename it, then add the hooks you still want in Settings > "
            "Tasks and hooks > Hooks.",
        )
        return normalize_config(None, source="user")
    _report_turn_end(raw, path)
    return normalize_config(raw, source="user")


def save_user_hooks(by_event: dict[str, list[HookDef]]) -> None:
    """Write the user layer as v2. Refuses to overwrite a v1 file, and keeps
    event keys the app does not read (an old "TurnEnd") as they are."""
    path = hooks_path()
    try:
        raw = _read_json(path)
    except (OSError, ValueError) as e:
        raise UnreadableHooksFile(
            f"{path} cannot be read ({e}), so it is not changed. Fix or move it, "
            "then add the hook again."
        ) from e
    if _is_v1(raw):
        raise OldHooksFile(
            f"{path} is in the old v1 format, so it is not changed. "
            "Move or rename it, then add the hook again."
        )
    doc = serialize_v2(by_event)
    stored = raw.get("hooks", raw) if isinstance(raw, dict) else None
    if isinstance(stored, dict):
        for ev, defs in stored.items():
            if ev not in HOOK_EVENTS and isinstance(defs, list):
                doc["hooks"][ev] = defs
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
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
    """Drop trust for a workspace's project hooks; they go inert again."""
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

    The trust check hashes the SAME ``raw`` that gets normalized and executed:
    a single read closes the TOCTOU where a concurrent writer could swap in
    malicious hooks between the trust check and the load."""
    raw = _project_hooks_raw(workspace)
    if not raw or not _raw_is_trusted(workspace, raw):
        return normalize_config(None, source="project")
    where = os.path.join(workspace or "", ".whisper", "settings.json")
    if _is_v1(raw):
        _notice(
            PROJECT_V1_NOTICE_TITLE,
            f'The "hooks" list in {where} is in the old v1 list format, which the app '
            "does not read, so none of those hooks run. The file is left as it is. "
            'Write "hooks" as an object of event names, each with a list of hooks, '
            "to run them.",
        )
        return normalize_config(None, source="project")
    _report_turn_end(raw, where)
    return normalize_config(raw, source="project")


def merged_for_event(event: str, workspace: str | None) -> list[HookDef]:
    """Enabled user + trusted-project shell hooks for an event, user first."""
    user = load_user_hooks().get(event, [])
    project = load_project_hooks(workspace).get(event, [])
    return [d for d in [*user, *project] if d.enabled]
