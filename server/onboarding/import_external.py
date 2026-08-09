"""Translate an existing Claude Code or Codex CLI project into whisper's own
native formats.

Whisper already has a native target for everything these tools bring:

  - a project instructions file, read by ``server/whisper_md.py`` as
    ``<workspace>/WHISPER.md`` (Claude Code: ``CLAUDE.md``; Codex: ``AGENTS.md``)
  - a folder-based skills system (``server/folder_skills.py``) that parses the
    identical ``SKILL.md`` YAML-frontmatter convention both tools use. Skills
    are an app-wide directory (``server.infrastructure.paths.skills_root()``),
    not a per-workspace one — there is no per-workspace skills concept in
    whisper today, so imported skills land there, same as every other skill.
  - an MCP server config store (``server/mcp.py``, ``mcp_servers.json``),
    which both tools also express as a name -> {command, args, env} map.

This module only locates the source files and writes/merges them into those
real locations. It never overwrites a WHISPER.md, an existing skill folder, or
an already-configured MCP server — conflicts are skipped and reported, not
clobbered. Nothing it writes is executed: WHISPER.md and skill bodies are
inert text until read through the normal chat/skill-loading path, exactly as
if the user had typed them by hand.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

from server import folder_skills, skills_import
from server.mcp import mcp_manager
from server.whisper_md import FILENAME as WHISPER_MD_FILENAME

log = logging.getLogger("whisper-studio")


class ExternalImportError(Exception):
    """Raised for a recoverable import failure (bad source/workspace path)."""


# --- public entry points -----------------------------------------------


def import_claude_project(source_path: str, workspace_path: str) -> dict:
    """Import a Claude Code project: ``CLAUDE.md``, ``.claude/skills/*``,
    ``.mcp.json``."""
    return _import_project(
        source_path,
        workspace_path,
        tool_label="Claude Code",
        instructions_filename="CLAUDE.md",
        skill_root_rels=[".claude/skills"],
        mcp_config_rels=[".mcp.json"],
        mcp_parser=_parse_claude_mcp_config,
    )


def import_codex_project(source_path: str, workspace_path: str) -> dict:
    """Import a Codex CLI project: ``AGENTS.md``, ``.codex/skills/*`` (or the
    ``.codex/.agents/skills/*`` layout some Codex setups use), and the
    ``[mcp_servers.*]`` tables in ``config.toml``."""
    return _import_project(
        source_path,
        workspace_path,
        tool_label="Codex",
        instructions_filename="AGENTS.md",
        skill_root_rels=[".codex/skills", ".codex/.agents/skills"],
        mcp_config_rels=[".codex/config.toml", "config.toml"],
        mcp_parser=_parse_codex_mcp_config,
    )


# --- shared driver -------------------------------------------------------


def _import_project(
    source_path: str,
    workspace_path: str,
    *,
    tool_label: str,
    instructions_filename: str,
    skill_root_rels: list[str],
    mcp_config_rels: list[str],
    mcp_parser,
) -> dict:
    source_path = os.path.abspath(os.path.expanduser(source_path or ""))
    workspace_path = os.path.abspath(os.path.expanduser(workspace_path or ""))
    if not os.path.isdir(source_path):
        raise ExternalImportError(
            f"source_path does not exist or is not a directory: {source_path}"
        )
    if not os.path.isdir(workspace_path):
        raise ExternalImportError(
            f"workspace_path does not exist or is not a directory: {workspace_path}"
        )

    return {
        "tool": tool_label,
        "source": source_path,
        "workspace": workspace_path,
        "instructions": _import_instructions(
            source_path, workspace_path, instructions_filename, tool_label
        ),
        "skills": _import_skills(source_path, skill_root_rels),
        "mcp": _import_mcp_servers(source_path, mcp_config_rels, mcp_parser),
    }


# --- instructions file: CLAUDE.md / AGENTS.md -> WHISPER.md --------------


def _import_instructions(
    source_path: str, workspace_path: str, filename: str, tool_label: str
) -> dict:
    src = os.path.join(source_path, filename)
    if not os.path.isfile(src):
        return {"status": "skipped", "reason": f"no {filename} found at {src}"}
    try:
        with open(src, errors="replace") as f:
            content = f.read().strip()
    except OSError as e:
        return {"status": "skipped", "reason": f"could not read {filename}: {e}"}
    if not content:
        return {"status": "skipped", "reason": f"{filename} is empty"}

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note = f"<!-- Migrated from {tool_label} ({filename}) on {stamp} -->\n\n"
    block = note + content

    dest = os.path.join(workspace_path, WHISPER_MD_FILENAME)
    if os.path.isfile(dest):
        try:
            with open(dest, errors="replace") as f:
                existing = f.read()
        except OSError as e:
            return {"status": "error", "reason": f"could not read existing WHISPER.md: {e}"}
        merged = existing.rstrip() + f"\n\n---\n\n## Imported from {tool_label}\n\n" + block + "\n"
        try:
            with open(dest, "w") as f:
                f.write(merged)
        except OSError as e:
            return {"status": "error", "reason": f"could not write WHISPER.md: {e}"}
        return {"status": "appended", "path": dest, "source": src}

    try:
        with open(dest, "w") as f:
            f.write(block + "\n")
    except OSError as e:
        return {"status": "error", "reason": f"could not write WHISPER.md: {e}"}
    return {"status": "written", "path": dest, "source": src}


# --- skills: <source>/<skill-root>/*/SKILL.md -> SKILLS_DIR --------------


def _find_skill_md(dir_path: str) -> str | None:
    for name in folder_skills.SKILL_MD_NAMES:
        p = os.path.join(dir_path, name)
        if os.path.isfile(p):
            return p
    return None


def _skill_content_differs(src_dir: str, dest_dir: str) -> bool:
    """Whether the destination's SKILL.md differs from the source's — used
    only to phrase a more useful skip reason, never to decide whether to
    overwrite (a conflict is always skipped)."""
    src_md = _find_skill_md(src_dir)
    dest_md = _find_skill_md(dest_dir)
    if not src_md or not dest_md:
        return True
    try:
        with open(src_md, errors="replace") as f:
            a = f.read().strip()
        with open(dest_md, errors="replace") as f:
            b = f.read().strip()
        return a != b
    except OSError:
        return True


def _copy_one_skill(skill_dir: str, folder_name: str) -> dict:
    skill = folder_skills.load_folder_skill(skill_dir)
    if not skill or not skill.get("name"):
        return {"name": folder_name, "status": "skipped", "reason": "unparseable SKILL.md"}
    skill_name = skill["name"]

    if skills_import._is_reserved(skill_name):
        return {
            "name": skill_name,
            "status": "skipped",
            "reason": f"reserved tool name: {skill_name}",
        }

    dest = skills_import._safe_dest_name(folder_name)
    if not dest:
        return {"name": skill_name, "status": "skipped", "reason": "invalid destination name"}

    if os.path.exists(dest):
        reason = (
            "already exists with different content"
            if _skill_content_differs(skill_dir, dest)
            else "already exists (identical)"
        )
        return {"name": skill_name, "status": "skipped", "reason": reason}

    try:
        skills_import._copy_skill_folder(skill_dir, dest)
    except skills_import.SkillImportError as e:
        return {"name": skill_name, "status": "skipped", "reason": str(e)}
    return {"name": skill_name, "status": "copied", "path": dest}


def _import_skills(source_path: str, skill_root_rels: list[str]) -> dict:
    os.makedirs(skills_import.SKILLS_DIR, exist_ok=True)
    copied: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()

    for rel in skill_root_rels:
        root = os.path.join(source_path, rel)
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            if entry in seen:
                continue
            skill_dir = os.path.join(root, entry)
            if not folder_skills.is_folder_skill(skill_dir):
                continue
            seen.add(entry)
            result = _copy_one_skill(skill_dir, entry)
            (copied if result["status"] == "copied" else skipped).append(result)

    return {"copied": copied, "skipped": skipped}


# --- MCP servers: source config -> mcp_servers.json (merge, never clobber) --


def _import_mcp_servers(source_path: str, config_rels: list[str], parser) -> dict:
    found_path = None
    parsed: dict[str, dict] = {}
    for rel in config_rels:
        candidate = os.path.join(source_path, rel)
        if os.path.isfile(candidate):
            found_path = candidate
            try:
                parsed = parser(candidate)
            except Exception as e:
                return {"added": [], "skipped": [], "source": candidate, "error": str(e)}
            break

    if found_path is None:
        return {"added": [], "skipped": [], "source": None, "reason": "no MCP server config found"}
    if not parsed:
        return {"added": [], "skipped": [], "source": found_path, "reason": "no servers defined"}

    config = mcp_manager.load_config()
    added: list[str] = []
    skipped: list[dict] = []
    changed = False
    for name, conf in parsed.items():
        if name in config:
            skipped.append({"name": name, "reason": "already configured"})
            continue
        config[name] = {
            "command": str(conf.get("command", "") or ""),
            "args": list(conf.get("args", []) or []),
            "env": dict(conf.get("env", {}) or {}),
            "enabled": True,
        }
        added.append(name)
        changed = True
    if changed:
        mcp_manager.save_config(config)
    return {"added": added, "skipped": skipped, "source": found_path}


def _parse_claude_mcp_config(path: str) -> dict[str, dict]:
    """Claude Code / Claude Desktop project MCP config: a JSON file whose
    ``mcpServers`` map is name -> {command, args, env}."""
    with open(path, errors="replace") as f:
        data = json.load(f)
    servers = data.get("mcpServers") or data.get("servers") or {}
    out = {}
    for name, conf in servers.items():
        if isinstance(conf, dict):
            out[str(name)] = conf
    return out


def _parse_codex_mcp_config(path: str) -> dict[str, dict]:
    """Codex CLI's config.toml: ``[mcp_servers.NAME]`` tables. Uses stdlib
    ``tomllib`` when available (Python 3.11+); whisper's runtime floor is
    3.10 (see pyproject.toml), so on 3.10 this falls back to a minimal
    hand-rolled extractor covering just the ``mcp_servers`` tables rather
    than adding a TOML dependency for one onboarding path."""
    try:
        import tomllib
    except ImportError:
        with open(path, errors="replace") as f:
            text = f.read()
        servers = parse_mcp_servers_toml_fallback(text)
    else:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        servers = data.get("mcp_servers") or {}

    out = {}
    for name, conf in servers.items():
        if isinstance(conf, dict):
            out[str(name)] = conf
    return out


# --- minimal TOML [mcp_servers.*] fallback (Python < 3.11 only) ---------

_SECTION_RE = re.compile(r"^\[mcp_servers\.(?P<name>\"[^\"]+\"|'[^']+'|[^.\]\s]+)\]\s*$")


def _split_top_level(s: str, sep: str) -> list[str]:
    """Split ``s`` on ``sep`` at bracket/brace depth 0, ignoring separators
    inside quoted strings or nested ``[...]`` / ``{...}``."""
    parts: list[str] = []
    depth = 0
    buf = ""
    in_str = False
    quote = ""
    for ch in s:
        if in_str:
            buf += ch
            if ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str = True
            quote = ch
            buf += ch
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


def _parse_toml_scalar(raw: str):
    val = raw.strip().rstrip(",").strip()
    if not val:
        return None
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    if val.startswith("[") and val.endswith("]"):
        return [_parse_toml_scalar(p) for p in _split_top_level(val[1:-1], ",") if p.strip()]
    if val.startswith("{") and val.endswith("}"):
        out = {}
        for part in _split_top_level(val[1:-1], ","):
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            out[k.strip().strip("\"'")] = _parse_toml_scalar(v)
        return out
    if val in ("true", "false"):
        return val == "true"
    return val


def parse_mcp_servers_toml_fallback(text: str) -> dict[str, dict]:
    """Minimal ``[mcp_servers.NAME]`` extractor for when ``tomllib`` is
    unavailable (Python < 3.11). Handles quoted or bare server names,
    string/array/inline-table values, and values that wrap across lines
    inside brackets/braces. Anything else in the file (other tables,
    comments, array-of-tables) is ignored rather than raising — a partial
    read is better than failing the whole import over an unrelated section
    this fallback doesn't understand."""
    servers: dict[str, dict] = {}
    current: dict | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _SECTION_RE.match(stripped)
        if m:
            name = m.group("name").strip().strip("\"'")
            current = {}
            servers[name] = current
            continue
        if stripped.startswith("["):
            # A different table entirely — stop collecting into `current`.
            current = None
            continue
        if current is None or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip()
        # A bracketed/braced value may wrap across following lines.
        while (val.count("[") + val.count("{")) > (val.count("]") + val.count("}")) and i < len(
            lines
        ):
            val += "\n" + lines[i]
            i += 1
        current[key] = _parse_toml_scalar(val)
    return servers
