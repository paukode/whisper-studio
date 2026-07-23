"""On-disk plan documents under data/plans/ (gitignored via the data/ rule).

The create_plan tool writes the full markdown here and the chat carries only a
summary + a card; GET /api/plans/{id} serves the markdown back to the dock's
plan panel. Plan ids are slug-based and validated on read to prevent path
traversal.
"""

from __future__ import annotations

import os
import re

from server.infrastructure.paths import data_root

_PLANS_DIR = os.path.join(data_root(), "plans")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or "plan"


def _session_tag(session_id: str | None) -> str:
    return (re.sub(r"[^a-z0-9]", "", (session_id or "").lower())[:8]) or "session"


def plan_id_for(session_id: str, title: str) -> str:
    """Stable, filesystem-safe id: <title-slug>-<session-tag>. Re-planning the
    same title in a session overwrites (updates) that plan."""
    return f"{_slugify(title)}-{_session_tag(session_id)}"


def _path_for(plan_id: str) -> str | None:
    if not _ID_RE.match(plan_id or ""):
        return None
    return os.path.join(_PLANS_DIR, f"{plan_id}.md")


def write_plan(session_id: str, title: str, markdown: str) -> dict:
    """Persist a plan's markdown; returns {id, path}."""
    os.makedirs(_PLANS_DIR, exist_ok=True)
    plan_id = plan_id_for(session_id, title)
    path = _path_for(plan_id)
    assert path is not None  # plan_id_for only emits _ID_RE-valid ids
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown or "")
    return {"id": plan_id, "path": path}


def read_plan(plan_id: str) -> str | None:
    """Return a plan's markdown, or None if the id is invalid/absent."""
    path = _path_for(plan_id)
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def list_plans(session_id: str | None = None) -> list[dict]:
    """List saved plans (optionally scoped to a session), newest first."""
    if not os.path.isdir(_PLANS_DIR):
        return []
    tag = _session_tag(session_id) if session_id else None
    out: list[dict] = []
    for name in os.listdir(_PLANS_DIR):
        if not name.endswith(".md"):
            continue
        pid = name[:-3]
        if tag and not pid.endswith(f"-{tag}"):
            continue
        out.append({"id": pid, "mtime": os.path.getmtime(os.path.join(_PLANS_DIR, name))})
    out.sort(key=lambda p: p["mtime"], reverse=True)
    return out
