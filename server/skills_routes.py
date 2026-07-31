"""FastAPI HTTP handlers for /api/skills/* and /api/whisper-md/*.

Split out of server/skills.py so that module can stay the skill loader +
tool-execution core. These handlers register on the two routers defined in
skills.py (``router`` and ``whisper_md_router``) via decorator side-effects;
skills.py imports this module for those effects.

Skill state (``SKILLS``, ``SKILLS_DIR``, config) is reached through the ``_sk``
module alias rather than by-value imports: ``SKILLS`` is reassigned by the
create/update/delete/import handlers, and tests monkeypatch ``SKILLS_DIR`` /
``SKILLS_CONFIG_PATH`` on the module, so a by-value import would freeze a stale
reference and defeat those patches.
"""

import json
import logging
import os

from fastapi import Request
from fastapi.responses import Response

import server.skills as _sk
from server import folder_skills
from server.mcp import mcp_manager
from server.skills import router, whisper_md_router

log = logging.getLogger("whisper-studio")


@router.get("")
async def skills_endpoint():
    cfg = _sk.load_skills_config()
    disabled = set(cfg.get("disabled", []))
    trusted = set(cfg.get("trusted", []))
    skills = [
        {
            "name": s["name"],
            "description": s["description"],
            "triggers": s["triggers"],
            "enabled": s["name"] not in disabled,
            "isFolder": bool(s.get("is_folder")),
            "hasScripts": bool(s.get("has_scripts")),
            "trusted": s["name"] in trusted,
        }
        for s in _sk.SKILLS.values()
    ]
    # Only tools from servers whose persisted `enabled` flag is on — the
    # autocomplete/skills UI must mirror the MCP ticks, not the connection
    # state (servers stay warm while disabled so re-enabling is instant).
    enabled_servers = mcp_manager.globally_enabled_servers()
    mcp_tools = []
    for tool_key, tool_info in mcp_manager._tools.items():
        if tool_info.get("server_name", "") not in enabled_servers:
            continue
        mcp_tool = tool_info["mcp_tool"]
        mcp_tools.append(
            {
                "name": tool_key,
                "description": mcp_tool.description or mcp_tool.name,
                "server": tool_info.get("server_name", ""),
            }
        )
    return {"skills": skills, "mcpTools": mcp_tools}


def _folder_skill_dir(name: str) -> str | None:
    """Return the on-disk realpath of a loaded folder skill, verified to sit
    inside SKILLS_DIR, or None if ``name`` is not a folder skill."""
    skill = _sk.SKILLS.get(name)
    if not skill or not skill.get("skill_dir"):
        return None
    root = os.path.realpath(_sk.SKILLS_DIR)
    real = os.path.realpath(skill["skill_dir"])
    if real == root or real.startswith(root + os.sep):
        return real
    return None


def _skill_md_in(skill_dir: str) -> str | None:
    for n in folder_skills.SKILL_MD_NAMES:
        p = os.path.join(skill_dir, n)
        if os.path.isfile(p):
            return p
    return None


@router.get("/{name}")
async def get_skill(name: str):
    skill = _sk.SKILLS.get(name)
    if not skill:
        return Response(
            content=json.dumps({"error": "Skill not found"}),
            status_code=404,
            media_type="application/json",
        )
    cfg = _sk.load_skills_config()
    disabled = cfg.get("disabled", [])
    # Folder skill: return the SKILL.md as read-only content plus its file tree.
    skill_dir = _folder_skill_dir(name)
    if skill_dir:
        file_content = ""
        md = _skill_md_in(skill_dir)
        if md:
            with open(md, errors="replace") as f:
                file_content = f.read()
        return {
            "name": name,
            "content": file_content,
            "enabled": name not in disabled,
            "description": skill.get("description", ""),
            "isFolder": True,
            "readOnly": True,
            "hasScripts": bool(skill.get("has_scripts")),
            "files": _list_asset_files(skill_dir),
            "trusted": name in cfg.get("trusted", []),
        }
    file_content = ""
    found = _sk._find_skill_file(name)
    if found:
        with open(os.path.join(_sk.SKILLS_DIR, found)) as f:
            file_content = f.read()
    return {
        "name": name,
        "content": file_content,
        "enabled": name not in disabled,
        "description": skill.get("description", ""),
        "isFolder": False,
    }


# Cap asset listing/serving so a huge bundled tree can't blow up the UI or memory.
_MAX_ASSET_FILES = 500
_MAX_ASSET_BYTES = 256 * 1024


def _list_asset_files(skill_dir: str) -> list[dict]:
    """Return a size-capped list of {path, size} for files under a folder skill,
    with POSIX-style relative paths. Skips dotfiles and unreadable entries."""
    out: list[dict] = []
    root = os.path.realpath(skill_dir)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append({"path": rel, "size": size})
            if len(out) >= _MAX_ASSET_FILES:
                return out
    return out


def _safe_asset_file(skill_dir: str, rel: str) -> str | None:
    """Resolve a relative asset path to an absolute file inside skill_dir, or
    None if it escapes the directory or is not a regular file."""
    rel = (rel or "").strip()
    if not rel:
        return None
    root = os.path.realpath(skill_dir)
    real = os.path.realpath(os.path.join(root, rel))
    if (real == root or real.startswith(root + os.sep)) and os.path.isfile(real):
        return real
    return None


@router.get("/{name}/files")
async def list_skill_files(name: str):
    skill_dir = _folder_skill_dir(name)
    if not skill_dir:
        return Response(
            content=json.dumps({"error": "Not a folder skill"}),
            status_code=404,
            media_type="application/json",
        )
    return {"name": name, "files": _list_asset_files(skill_dir)}


@router.get("/{name}/file")
async def read_skill_file(name: str, path: str):
    skill_dir = _folder_skill_dir(name)
    if not skill_dir:
        return Response(
            content=json.dumps({"error": "Not a folder skill"}),
            status_code=404,
            media_type="application/json",
        )
    real = _safe_asset_file(skill_dir, path)
    if not real:
        return Response(
            content=json.dumps({"error": "Invalid path"}),
            status_code=400,
            media_type="application/json",
        )
    try:
        with open(real, "rb") as f:
            raw = f.read(_MAX_ASSET_BYTES + 1)
    except OSError as e:
        return Response(
            content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json"
        )
    truncated = len(raw) > _MAX_ASSET_BYTES
    raw = raw[:_MAX_ASSET_BYTES]
    try:
        content = raw.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        content = ""
        binary = True
    return {
        "name": name,
        "path": path,
        "content": content,
        "truncated": truncated,
        "binary": binary,
    }


def _json_error(msg: str, status: int):
    return Response(
        content=json.dumps({"error": msg}), status_code=status, media_type="application/json"
    )


@router.post("/import/preview")
async def import_preview(request: Request):
    """List the skills a git repo offers, without downloading their contents."""
    from server import skills_import

    body = await request.json()
    try:
        return skills_import.preview(body.get("url", ""))
    except skills_import.SkillImportError as e:
        return _json_error(str(e), 400)
    except Exception as e:
        log.warning("skill import preview failed: %s", e)
        return _json_error(f"preview failed: {e}", 500)


@router.post("/import")
async def import_skills_endpoint(request: Request):
    """Sparse-fetch selected skill folders from a git URL into /skills/ and
    hot-reload — no app restart."""
    from server import skills_import

    body = await request.json()
    try:
        result = skills_import.import_skills(
            body.get("url", ""),
            body.get("subpaths", []),
            bool(body.get("overwrite", False)),
        )
    except skills_import.SkillImportError as e:
        return _json_error(str(e), 400)
    except Exception as e:
        log.warning("skill import failed: %s", e)
        return _json_error(f"import failed: {e}", 500)
    _sk.SKILLS = _sk.load_skills()
    _sk.rebuild_tools()
    return result


def _safe_skill_filepath(name: str) -> str | None:
    """Resolve a user-supplied skill name to a ``.md`` path *inside* SKILLS_DIR,
    or None if the name would escape it (path traversal). Reject names that
    contain path separators or ``..`` outright, then confirm containment with
    realpath as defense-in-depth."""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or os.sep in name or ".." in name:
        return None
    filepath = os.path.join(_sk.SKILLS_DIR, name.replace("_", "-") + ".md")
    root = os.path.realpath(_sk.SKILLS_DIR)
    real = os.path.realpath(filepath)
    if real == root or real.startswith(root + os.sep):
        return filepath
    return None


@router.post("")
async def create_skill(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    content = body.get("content", "").strip()
    if not name or not content:
        return Response(
            content=json.dumps({"error": "name and content are required"}),
            status_code=400,
            media_type="application/json",
        )
    filepath = _safe_skill_filepath(name)
    if not filepath:
        return Response(
            content=json.dumps({"error": "Invalid skill name"}),
            status_code=400,
            media_type="application/json",
        )
    if os.path.exists(filepath):
        return Response(
            content=json.dumps({"error": "Skill file already exists"}),
            status_code=409,
            media_type="application/json",
        )
    with open(filepath, "w") as f:
        f.write(content)
    _sk.SKILLS = _sk.load_skills()
    _sk.rebuild_tools()
    return {"name": name, "created": True}


@router.put("/{name}")
async def update_skill(name: str, request: Request):
    body = await request.json()
    new_content = body.get("content", "").strip()
    new_name = body.get("new_name", "").strip()
    if not new_content:
        return Response(
            content=json.dumps({"error": "content is required"}),
            status_code=400,
            media_type="application/json",
        )
    if _folder_skill_dir(name):
        return Response(
            content=json.dumps(
                {"error": "Folder skills are read-only; edit the files on disk or re-import"}
            ),
            status_code=400,
            media_type="application/json",
        )
    old_file = _sk._find_skill_file(name)
    if not old_file:
        return Response(
            content=json.dumps({"error": "Skill not found"}),
            status_code=404,
            media_type="application/json",
        )
    if new_name and new_name != name:
        new_filepath = _safe_skill_filepath(new_name)
        if not new_filepath:
            return Response(
                content=json.dumps({"error": "Invalid skill name"}),
                status_code=400,
                media_type="application/json",
            )
        with open(new_filepath, "w") as f:
            f.write(new_content)
        os.remove(os.path.join(_sk.SKILLS_DIR, old_file))
        config = _sk.load_skills_config()
        if name in config.get("disabled", []):
            config["disabled"].remove(name)
            config["disabled"].append(new_name)
            _sk.save_skills_config(config)
    else:
        with open(os.path.join(_sk.SKILLS_DIR, old_file), "w") as f:
            f.write(new_content)
    _sk.SKILLS = _sk.load_skills()
    _sk.rebuild_tools()
    return {"name": new_name or name, "updated": True}


@router.patch("/{name}/toggle")
async def toggle_skill(name: str):
    config = _sk.load_skills_config()
    disabled = config.get("disabled", [])
    if name in disabled:
        disabled.remove(name)
        enabled = True
    else:
        disabled.append(name)
        enabled = False
    config["disabled"] = disabled
    _sk.save_skills_config(config)
    _sk.rebuild_tools()
    return {"name": name, "enabled": enabled}


@router.patch("/{name}/trust")
async def trust_skill(name: str):
    """Toggle a skill's `trusted` flag. Trust gates `allowed-tools`
    auto-approval for folder skills (see the permissions layer)."""
    config = _sk.load_skills_config()
    trusted = config.get("trusted", [])
    if name in trusted:
        trusted.remove(name)
        is_trusted = False
    else:
        trusted.append(name)
        is_trusted = True
    config["trusted"] = trusted
    _sk.save_skills_config(config)
    return {"name": name, "trusted": is_trusted}


@router.delete("/{name}")
async def delete_skill(name: str):
    # Folder skill: remove the whole directory.
    skill_dir = _folder_skill_dir(name)
    if skill_dir:
        import shutil

        shutil.rmtree(skill_dir, ignore_errors=True)
    else:
        old_file = _sk._find_skill_file(name)
        if not old_file:
            return Response(
                content=json.dumps({"error": "Skill not found"}),
                status_code=404,
                media_type="application/json",
            )
        os.remove(os.path.join(_sk.SKILLS_DIR, old_file))
    config = _sk.load_skills_config()
    changed = False
    for key in ("disabled", "trusted"):
        if name in config.get(key, []):
            config[key].remove(name)
            changed = True
    if changed:
        _sk.save_skills_config(config)
    _sk.SKILLS = _sk.load_skills()
    _sk.rebuild_tools()
    return {"name": name, "deleted": True}


# --- WHISPER.md API (Feature 10 + 18) ---


@whisper_md_router.get("")
async def get_whisper_md():
    """Get the WHISPER.md file from the connected workspace."""
    from server.workspace import get_workspace_path

    ws = get_workspace_path()
    if not ws:
        return {"content": "", "exists": False}
    path = os.path.join(ws, "WHISPER.md")
    if not os.path.isfile(path):
        return {"content": "", "exists": False, "path": path}
    try:
        with open(path, errors="replace") as f:
            content = f.read()
        return {"content": content, "exists": True, "path": path}
    except Exception as e:
        return {"content": "", "exists": False, "error": str(e)}


@whisper_md_router.put("")
async def save_whisper_md(request: Request):
    """Save WHISPER.md to the connected workspace."""
    from server.workspace import get_workspace_path

    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace connected"}),
            status_code=400,
            media_type="application/json",
        )
    body = await request.json()
    content = body.get("content", "")
    path = os.path.join(ws, "WHISPER.md")
    try:
        with open(path, "w") as f:
            f.write(content)
        return {"saved": True, "path": path}
    except Exception as e:
        return Response(
            content=json.dumps({"error": str(e)}), status_code=500, media_type="application/json"
        )


@whisper_md_router.delete("")
async def delete_whisper_md():
    """Delete WHISPER.md from the connected workspace."""
    from server.workspace import get_workspace_path

    ws = get_workspace_path()
    if not ws:
        return Response(
            content=json.dumps({"error": "No workspace connected"}),
            status_code=400,
            media_type="application/json",
        )
    path = os.path.join(ws, "WHISPER.md")
    if os.path.isfile(path):
        os.remove(path)
    return {"deleted": True}
