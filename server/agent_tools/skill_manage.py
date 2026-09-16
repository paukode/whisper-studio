"""skill_manage: the assistant authors and maintains its own folder skills.

Skills are the assistant's procedural memory: how to do a class of task the
way this user wants it. Until now only the Settings UI and the git importer
could create or edit one; the model could list and invoke. This executor
gives it the write side, under the same guards the HTTP routes use: names
are validated and checked against the reserved tool namespace, every path is
resolved inside the skill's own directory, files are size-capped, and a skill
the assistant did not create cannot be deleted from chat (edit it, or remove
it in Settings).

Actions: view, create, patch, append, write_file, remove_file, delete.
Agent-created skills carry ``created_by: agent`` in their frontmatter.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone

import server.skills as _sk
from server import folder_skills

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_SUPPORT_DIRS = ("references", "scripts", "templates", "assets")
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_SUPPORT_FILE_BYTES = 200 * 1024
MAX_DESCRIPTION_CHARS = 200
_MAX_VIEW_CHARS = 60_000


def _err(msg: str, **extra) -> str:
    return json.dumps({"error": msg, **extra})


def _ok(**payload) -> str:
    return json.dumps({"ok": True, **payload})


def _enabled() -> bool:
    try:
        from server.infrastructure.feature_flags import is_enabled

        return is_enabled("skill_self_improvement")
    except Exception:  # noqa: BLE001
        return True


def _reload() -> None:
    _sk.SKILLS = _sk.load_skills()
    _sk.rebuild_tools()


def _skill_dir_for(name: str) -> str | None:
    skill = _sk.SKILLS.get(name) or _sk.SKILLS.get(name.replace("-", "_"))
    if skill and skill.get("skill_dir"):
        return skill["skill_dir"]
    candidate = os.path.join(_sk.SKILLS_DIR, name)
    if folder_skills.is_folder_skill(candidate):
        return os.path.realpath(candidate)
    return None


def _skill_md(skill_dir: str) -> str | None:
    for fn in ("SKILL.md", "skill.md"):
        p = os.path.join(skill_dir, fn)
        if os.path.isfile(p):
            return p
    return None


def _flat_skill_file(name: str) -> str | None:
    if not os.path.isdir(_sk.SKILLS_DIR):
        return None
    rel = _sk._find_skill_file(name)
    return os.path.join(_sk.SKILLS_DIR, rel) if rel else None


def _validate_name(name: str) -> str | None:
    """Return an error string, or None when the name is acceptable."""
    if not _NAME_RE.match(name or ""):
        return (
            "name must be 2-64 chars: lowercase letters, digits, '_' or '-', starting with a letter"
        )
    try:
        from server.skills_import import _is_reserved

        if _is_reserved(name.replace("-", "_")):
            return f"'{name}' collides with a built-in tool name; pick another"
    except Exception:  # noqa: BLE001
        pass
    root = os.path.realpath(_sk.SKILLS_DIR)
    real = os.path.realpath(os.path.join(_sk.SKILLS_DIR, name))
    if not real.startswith(root + os.sep):
        return "invalid skill name"
    return None


def _frontmatter(skill_md_path: str) -> dict:
    try:
        with open(skill_md_path, encoding="utf-8", errors="replace") as f:
            fm, _body = folder_skills.parse_frontmatter(f.read())
        return fm or {}
    except (OSError, ValueError):
        return {}


def _agent_created(skill_md_path: str) -> bool:
    return str(_frontmatter(skill_md_path).get("created_by", "")).strip().lower() == "agent"


def _support_path(skill_dir: str, rel: str) -> str | None:
    """Absolute path for a support file under references/, scripts/,
    templates/ or assets/ inside the skill, else None."""
    rel = (rel or "").strip().lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    top = rel.split("/", 1)[0]
    if top not in _SUPPORT_DIRS or "/" not in rel:
        return None
    return folder_skills._contained(skill_dir, rel)


def _render_skill_md(name: str, description: str, body: str, triggers: list[str]) -> str:
    lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if triggers:
        lines.append("triggers: " + ", ".join(triggers))
    lines += [
        "created_by: agent",
        f"created_at: {datetime.now(timezone.utc).date().isoformat()}",
        "---",
        "",
        body.strip(),
        "",
    ]
    return "\n".join(lines)


def _files(skill_dir: str) -> list[dict]:
    from server.skills_routes import _list_asset_files

    return _list_asset_files(skill_dir)


def _view(name: str) -> str:
    skill_dir = _skill_dir_for(name)
    if skill_dir:
        md = _skill_md(skill_dir)
        if not md:
            return _err(f"skill '{name}' has no SKILL.md")
        with open(md, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return json.dumps(
            {
                "name": name,
                "kind": "folder",
                "path": md,
                "created_by_agent": _agent_created(md),
                "content": content[:_MAX_VIEW_CHARS],
                "files": _files(skill_dir),
            }
        )
    flat = _flat_skill_file(name)
    if flat:
        with open(flat, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return json.dumps(
            {"name": name, "kind": "file", "path": flat, "content": content[:_MAX_VIEW_CHARS]}
        )
    return _err(f"no skill named '{name}'. Call skill_list to see the available names.")


def _create(name: str, tool_input: dict) -> str:
    err = _validate_name(name)
    if err:
        return _err(err)
    description = str(tool_input.get("description") or "").strip()
    body = str(tool_input.get("content") or "").strip()
    if not description or not body:
        return _err("create needs a description and content (the SKILL.md body)")
    if len(description) > MAX_DESCRIPTION_CHARS:
        return _err(
            f"description must be at most {MAX_DESCRIPTION_CHARS} characters; it is the index line"
        )
    if _skill_dir_for(name) or _flat_skill_file(name) or name in _sk.SKILLS:
        return _err(f"a skill named '{name}' already exists; use patch or append to change it")
    triggers_raw = tool_input.get("triggers") or []
    if isinstance(triggers_raw, str):
        triggers_raw = [t for t in triggers_raw.split(",")]
    triggers = [str(t).strip() for t in triggers_raw if str(t).strip()][:12]
    text = _render_skill_md(name, description, body, triggers)
    if len(text.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        return _err(f"SKILL.md exceeds {MAX_SKILL_MD_BYTES} bytes; move depth into references/")
    skill_dir = os.path.join(_sk.SKILLS_DIR, name)
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(text)
    _reload()
    return _ok(action="create", name=name, path=os.path.join(skill_dir, "SKILL.md"))


def _target_md(name: str) -> tuple[str | None, str | None]:
    """(path, error) for the editable markdown of a skill."""
    skill_dir = _skill_dir_for(name)
    if skill_dir:
        md = _skill_md(skill_dir)
        return (md, None) if md else (None, f"skill '{name}' has no SKILL.md")
    flat = _flat_skill_file(name)
    if flat:
        return flat, None
    return None, f"no skill named '{name}'"


def _patch(name: str, tool_input: dict) -> str:
    path, err = _target_md(name)
    if err:
        return _err(err)
    old = str(tool_input.get("old_text") or "")
    new = str(tool_input.get("new_text") if tool_input.get("new_text") is not None else "")
    if not old:
        return _err("patch needs old_text (a unique substring of the current skill text)")
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    n = text.count(old)
    if n == 0:
        return _err("old_text was not found; call skill_manage action=view and copy the exact text")
    if n > 1:
        return _err(f"old_text matches {n} places; include more surrounding text so it is unique")
    updated = text.replace(old, new, 1)
    if len(updated.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        return _err(f"skill text would exceed {MAX_SKILL_MD_BYTES} bytes")
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    _reload()
    return _ok(action="patch", name=name, path=path)


def _append(name: str, tool_input: dict) -> str:
    path, err = _target_md(name)
    if err:
        return _err(err)
    addition = str(tool_input.get("content") or "").strip()
    if not addition:
        return _err("append needs content")
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    updated = text.rstrip("\n") + "\n\n" + addition + "\n"
    if len(updated.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        return _err(f"skill text would exceed {MAX_SKILL_MD_BYTES} bytes")
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    _reload()
    return _ok(action="append", name=name, path=path)


def _write_file(name: str, tool_input: dict) -> str:
    skill_dir = _skill_dir_for(name)
    if not skill_dir:
        return _err(
            f"'{name}' is not a folder skill; support files need a folder skill (create one)"
        )
    rel = str(tool_input.get("path") or "")
    target = _support_path(skill_dir, rel)
    if not target:
        return _err("path must be inside references/, scripts/, templates/ or assets/ of the skill")
    content = tool_input.get("content")
    if not isinstance(content, str) or not content:
        return _err("write_file needs text content")
    if len(content.encode("utf-8")) > MAX_SUPPORT_FILE_BYTES:
        return _err(f"file exceeds {MAX_SUPPORT_FILE_BYTES} bytes")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    _reload()
    return _ok(action="write_file", name=name, path=target)


def _remove_file(name: str, tool_input: dict) -> str:
    skill_dir = _skill_dir_for(name)
    if not skill_dir:
        return _err(f"'{name}' is not a folder skill")
    target = _support_path(skill_dir, str(tool_input.get("path") or ""))
    if not target or not os.path.isfile(target):
        return _err("no such support file in this skill")
    os.remove(target)
    _reload()
    return _ok(action="remove_file", name=name, path=target)


def _delete(name: str) -> str:
    skill_dir = _skill_dir_for(name)
    if not skill_dir:
        if _flat_skill_file(name):
            return _err("this skill was written by the user; remove it in Settings > Skills")
        return _err(f"no skill named '{name}'")
    md = _skill_md(skill_dir)
    if not md or not _agent_created(md):
        return _err(
            "only skills the assistant created (created_by: agent) can be deleted from chat; "
            "edit this one, or remove it in Settings > Skills"
        )
    shutil.rmtree(skill_dir, ignore_errors=True)
    _reload()
    return _ok(action="delete", name=name)


def execute_skill_manage(tool_input: dict, session_id: str = "") -> str:
    if not _enabled():
        return _err("skill_manage is disabled (feature flag skill_self_improvement is off)")
    action = str(tool_input.get("action") or "").strip().lower()
    name = str(tool_input.get("name") or "").strip()
    if not name:
        return _err("name is required")
    if action == "view":
        return _view(name)
    if action == "create":
        return _create(name, tool_input)
    if action == "patch":
        return _patch(name, tool_input)
    if action == "append":
        return _append(name, tool_input)
    if action == "write_file":
        return _write_file(name, tool_input)
    if action == "remove_file":
        return _remove_file(name, tool_input)
    if action == "delete":
        return _delete(name)
    return _err(
        "action must be one of view, create, patch, append, write_file, remove_file, delete"
    )


__all__ = ["execute_skill_manage"]
