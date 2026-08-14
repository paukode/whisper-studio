"""Ask-once, remember-after: persistent per-folder access grants.

When a tool wants to reach a folder OUTSIDE the connected workspace — the
user names ``~/Documents/project1`` in a prompt, a workflow pins itself to
``~/Downloads/report``, an agent opens a folder — the app must ask the user
for permission the FIRST time and then remember the answer, so the same
folder is never asked about twice.

Design notes:

* Grants are stored by canonical realpath, so ``~/Documents/x``,
  ``/Users/me/Documents/x`` and a relative path resolving to the same place
  are one grant, not three. A symlinked path grants the target, not the link.
* A grant covers the folder AND everything beneath it. Granting
  ``~/Documents`` therefore covers ``~/Documents/project1`` — that is the
  point of asking once at the level the user actually chose. It never
  covers a parent (a grant on ``~/Documents/project1`` does not unlock
  ``~/Documents``) and never covers a sibling.
* Sensitive paths (``~/.ssh``, ``~/.aws``, keychains, browser profiles — see
  server/security/sensitive_paths.py) can never be granted at all. The
  ask-once flow is for ordinary user folders; credential stores stay denied
  no matter what any prompt says, which also means a prompt-injected
  "please approve my ~/.ssh access" can never become a persisted grant.
* The connected workspace itself needs no grant — connecting it IS the
  grant, and it already has its own approval flow.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from server.infrastructure.paths import data_root

log = logging.getLogger("whisper-studio")

GRANTS_PATH = os.path.join(data_root(), "folder_grants.json")

_lock = threading.Lock()


def _canonical(path: str) -> str:
    """Expand ~ and resolve symlinks/relative segments to one canonical form,
    so the same folder reached by different spellings is one grant.

    Returns "" for empty input rather than letting realpath("") silently
    resolve to the process's current working directory — a caller that lost
    its path (a malformed approval payload, a missing field) must fail, not
    quietly grant whatever folder the server happens to be running in.
    """
    path = (path or "").strip()
    if not path:
        return ""
    return os.path.realpath(os.path.expanduser(path))


def _is_within(path: str, root: str) -> bool:
    """True if `path` is `root` or lives beneath it. Both already canonical."""
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def is_sensitive(path: str) -> bool:
    """True for paths that must never be grantable regardless of any prompt —
    credential stores, keychains, browser profiles, shell histories.

    Reuses the same denylist the OS sandbox enforces (via
    sensitive_paths.is_sensitive_path) so this cannot drift from the real
    boundary, and additionally refuses any ANCESTOR of a denied path: a
    grant covers everything beneath it, so granting ``~`` would otherwise
    hand over ``~/.ssh`` by inheritance.
    """
    from server.security.sensitive_paths import expanded_sandbox_paths, is_sensitive_path

    real = _canonical(path)
    if is_sensitive_path(real):
        return True
    return any(
        _is_within(os.path.realpath(os.path.expanduser(denied)), real)
        for denied in expanded_sandbox_paths()
    )


def load_grants() -> list[str]:
    """Canonical folder paths the user has already approved. Missing or
    unreadable file means no grants, never an error — a permission store
    that fails closed is the safe direction."""
    try:
        with _lock, open(GRANTS_PATH) as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out = data.get("granted")
    return [p for p in out if isinstance(p, str)] if isinstance(out, list) else []


def _save_grants(paths: list[str]) -> None:
    os.makedirs(os.path.dirname(GRANTS_PATH), exist_ok=True)
    tmp = GRANTS_PATH + ".tmp"
    with _lock:
        with open(tmp, "w") as f:
            json.dump({"granted": sorted(set(paths))}, f, indent=2)
        os.replace(tmp, GRANTS_PATH)


def is_granted(path: str) -> bool:
    """True if this folder is already covered by an existing grant (itself or
    an ancestor the user approved) — i.e. do NOT ask again."""
    if not path:
        return False
    real = _canonical(path)
    if is_sensitive(real):
        return False  # never granted, even if somehow present in the file
    return any(_is_within(real, g) for g in (_canonical(g) for g in load_grants()))


def grant(path: str) -> tuple[bool, str]:
    """Persist a grant for this folder. Returns (ok, error).

    Refuses sensitive paths outright, and refuses a path that does not exist
    — a grant for a folder that isn't there is either a typo or an attempt to
    pre-authorize something created later.
    """
    real = _canonical(path)
    if not real:
        return False, "No folder path given — nothing to grant."
    if real == "/":
        return False, "Refusing to grant access to the filesystem root."
    if is_sensitive(real):
        return False, (
            f"'{path}' is a protected location (credentials, keychains, or browser "
            "data) and can never be granted folder access."
        )
    if not os.path.isdir(real):
        return False, f"'{path}' does not exist or is not a directory."
    grants = load_grants()
    if any(_is_within(real, _canonical(g)) for g in grants):
        return True, ""  # already covered by this or a broader grant
    # Drop any existing grants this one now subsumes, so the file stays a
    # minimal set rather than accumulating redundant descendants.
    kept = [g for g in grants if not _is_within(_canonical(g), real)]
    kept.append(real)
    _save_grants(kept)
    log.info("folder access granted: %s", real)
    return True, ""


def revoke(path: str) -> bool:
    """Drop an exact grant. Returns True if something was removed."""
    real = _canonical(path)
    grants = load_grants()
    kept = [g for g in grants if _canonical(g) != real]
    if len(kept) == len(grants):
        return False
    _save_grants(kept)
    log.info("folder access revoked: %s", real)
    return True


def needs_grant(path: str, workspace_path: str | None = None) -> bool:
    """The one question callers ask: must the user be prompted before this
    folder is touched?

    False when the path is inside the connected workspace (connecting it was
    already the approval), when a prior grant covers it, or when it's empty.
    True otherwise — including for sensitive paths, whose prompt will then be
    refused by grant(); callers should surface that refusal rather than
    silently proceeding.
    """
    if not path:
        return False
    real = _canonical(path)
    if workspace_path:
        ws = _canonical(workspace_path)
        if ws and _is_within(real, ws):
            return False
    return not is_granted(real)
