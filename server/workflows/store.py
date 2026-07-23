"""Saved-workflow store: named scripts the model can invoke by name (like skills).

Layout: ``data_root()/workflows/scripts/<name>/{workflow.mjs, meta.json}``.
meta.json = {name, description, phases, script_hash, trusted}. A saved workflow
is INERT for auto-run until trusted; editing the script (hash mismatch) drops
trust, exactly like project hooks / trusted skills — so a name can never
silently start executing changed code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from server.infrastructure.paths import data_root

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def scripts_root() -> str:
    return os.path.join(data_root(), "workflows", "scripts")


def valid_name(name: str) -> bool:
    """Kebab/snake slug only — blocks path traversal and odd filenames."""
    return bool(name) and bool(_NAME_RE.match(name))


def _dir(name: str) -> str:
    return os.path.join(scripts_root(), name)


def script_hash(script: str) -> str:
    return hashlib.sha256((script or "").encode("utf-8")).hexdigest()


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_script(name: str, script: str, meta: dict, *, trusted: bool = False) -> dict:
    """Persist (or replace) a named workflow. ``meta`` is the parsed
    {name, description, phases}. Returns the stored meta.json contents."""
    if not valid_name(name):
        raise ValueError(f"invalid workflow name: {name!r}")
    d = _dir(name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "workflow.mjs"), "w", encoding="utf-8") as f:
        f.write(script)
    stored = {
        "name": name,
        "description": str(meta.get("description", "")),
        "phases": list(meta.get("phases", []) or []),
        "script_hash": script_hash(script),
        "trusted": bool(trusted),
    }
    tmp = os.path.join(d, "meta.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=2)
    os.replace(tmp, os.path.join(d, "meta.json"))
    return stored


def load_script(name: str) -> dict | None:
    """Return {name, script, meta, trusted} for a saved workflow, or None."""
    if not valid_name(name):
        return None
    d = _dir(name)
    try:
        with open(os.path.join(d, "workflow.mjs"), encoding="utf-8") as f:
            script = f.read()
    except OSError:
        return None
    meta = _read_json(os.path.join(d, "meta.json")) or {}
    # Trust holds only while the on-disk script still matches the approved hash.
    trusted = bool(meta.get("trusted")) and meta.get("script_hash") == script_hash(script)
    return {"name": name, "script": script, "meta": meta, "trusted": trusted}


def list_scripts() -> list[dict]:
    """All saved workflows as {name, description, phases, trusted}."""
    root = scripts_root()
    if not os.path.isdir(root):
        return []
    out: list[dict] = []
    for name in sorted(os.listdir(root)):
        loaded = load_script(name)
        if not loaded:
            continue
        meta = loaded["meta"]
        out.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "phases": meta.get("phases", []),
                "trusted": loaded["trusted"],
            }
        )
    return out


def approve_script(name: str) -> bool:
    """Trust a saved workflow's CURRENT script (records its hash)."""
    loaded = load_script(name)
    if not loaded:
        return False
    meta = dict(loaded["meta"])
    meta["trusted"] = True
    meta["script_hash"] = script_hash(loaded["script"])
    d = _dir(name)
    tmp = os.path.join(d, "meta.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, os.path.join(d, "meta.json"))
    return True


def delete_script(name: str) -> bool:
    if not valid_name(name):
        return False
    d = _dir(name)
    if not os.path.isdir(d):
        return False
    for fn in ("workflow.mjs", "meta.json", "meta.json.tmp"):
        try:
            os.remove(os.path.join(d, fn))
        except OSError:
            pass
    try:
        os.rmdir(d)
    except OSError:
        pass
    return True
