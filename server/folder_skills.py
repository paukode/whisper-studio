"""Folder-based skills: discovery, frontmatter parsing, and body resolution.

A folder skill is a directory under ``/skills/`` that contains a ``SKILL.md``
(standard Claude frontmatter) plus optional ``scripts/`` (stdlib Python CLI
tools), ``references/`` (docs), and ``assets/``. Kept separate from skills.py so
the loader stays under the file-size budget.

Folder skills have no whisper-specific ``executor``, so they default to inline
prompt skills: their ``SKILL.md`` body becomes the tool result, with the skill's
own absolute directory injected so the model can run bundled scripts by absolute
path via the existing terminal_run / run_python executors.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("whisper-studio")

SKILL_MD_NAMES = ("SKILL.md", "skill.md")

# Bare relative references we rewrite to absolute paths as a fallback, for
# authors who wrote `scripts/foo.py` without the ${CLAUDE_SKILL_DIR} placeholder.
_REL_REF_RE = re.compile(r"(?<![\w/.])((?:scripts|references|assets)/[\w./\-]+)")

_SKILL_DIR_VAR = "${CLAUDE_SKILL_DIR}"
_SESSION_ID_VAR = "${CLAUDE_SESSION_ID}"


def _skill_md_path(dir_path: str) -> str | None:
    for name in SKILL_MD_NAMES:
        p = os.path.join(dir_path, name)
        if os.path.isfile(p):
            return p
    return None


def is_folder_skill(dir_path: str) -> bool:
    """True iff ``dir_path`` is a directory containing a SKILL.md / skill.md."""
    return os.path.isdir(dir_path) and _skill_md_path(dir_path) is not None


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split a SKILL.md into ``(frontmatter, body)``.

    Uses PyYAML when available (handles ``|``/``>`` block scalars, quoted
    multi-line values, nested maps, and list values); falls back to a tolerant
    flat parser otherwise. Returns ``({}, content)`` when there is no ``---``
    frontmatter block.
    """
    if not content.startswith("---"):
        return {}, content.strip()
    try:
        end = content.index("---", 3)
    except ValueError:
        return {}, content.strip()
    fm_text = content[3:end].strip()
    body = content[end + 3 :].strip()
    return _yaml_or_flat(fm_text), body


def _yaml_or_flat(fm_text: str) -> dict:
    try:
        import yaml

        data = yaml.safe_load(fm_text)
        if isinstance(data, dict):
            return data
    except Exception as e:  # malformed YAML or PyYAML absent
        log.debug("SKILL.md YAML parse failed, using flat fallback: %s", e)
    return _flat_frontmatter(fm_text)


def _flat_frontmatter(fm_text: str) -> dict:
    """Minimal flat-key parser with block-scalar (``|`` / ``>``) support.

    Only used when PyYAML is unavailable or the YAML is malformed. Unlike the
    legacy skills.py parser it does not drop the value of a ``key: |`` block.
    """
    fm: dict = {}
    lines = fm_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith(" ") or ":" not in line:
            i += 1
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if val in ("|", ">", "|-", ">-", "|+", ">+"):
            collected = []
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
                collected.append(lines[i].strip())
                i += 1
            joiner = "\n" if val.startswith("|") else " "
            fm[key] = joiner.join(collected).strip()
            continue
        fm[key] = val
        i += 1
    return fm


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _default_input_schema() -> dict:
    # Claude skills declare no input schema. Provide one optional free-text
    # property so Bedrock never receives an empty-object schema.
    return {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Optional request or arguments for the skill.",
            },
        },
        "required": [],
    }


def _normalize_name(raw, dir_name: str) -> str:
    name = (str(raw).strip() if raw else "") or dir_name
    return name.replace("-", "_")


def _has_scripts(skill_dir: str) -> bool:
    scripts_dir = os.path.join(skill_dir, "scripts")
    if not os.path.isdir(scripts_dir):
        return False
    try:
        return any(fn.endswith(".py") for fn in os.listdir(scripts_dir))
    except OSError:
        return False


def load_folder_skill(dir_path: str) -> dict | None:
    """Load one folder-skill directory into a SKILLS-dict entry (same shape as
    skills.py builds for ``.md`` skills, plus folder-specific fields), or None
    if it is not a valid folder skill."""
    md = _skill_md_path(dir_path)
    if not md:
        return None
    try:
        with open(md, errors="replace") as f:
            content = f.read()
    except OSError as e:
        log.warning("folder skill unreadable at %s: %s", md, e)
        return None
    fm, body = parse_frontmatter(content)
    dir_name = os.path.basename(os.path.normpath(dir_path))
    skill_dir = os.path.realpath(dir_path)
    model = fm.get("model")
    return {
        "name": _normalize_name(fm.get("name"), dir_name),
        "description": str(fm.get("description", "") or "").strip(),
        "triggers": _as_list(fm.get("triggers")),
        "executor": str(fm.get("executor", "") or "").strip(),
        "model": (str(model).strip() or None) if model else None,
        "body": body,
        "input_schema": _default_input_schema(),
        "is_folder": True,
        "skill_dir": skill_dir,
        "has_scripts": _has_scripts(skill_dir),
        "allowed_tools": _as_list(fm.get("allowed-tools") or fm.get("allowed_tools")),
        "license": str(fm.get("license", "") or "").strip(),
    }


def discover_folder_skills(skills_dir: str) -> dict[str, dict]:
    """Find every folder skill directly under ``skills_dir``."""
    out: dict[str, dict] = {}
    if not os.path.isdir(skills_dir):
        return out
    for entry in sorted(os.listdir(skills_dir)):
        dpath = os.path.join(skills_dir, entry)
        if not is_folder_skill(dpath):
            continue
        skill = load_folder_skill(dpath)
        if skill:
            out[skill["name"]] = skill
            log.info("Loaded folder skill: %s (scripts=%s)", skill["name"], skill["has_scripts"])
    return out


def _contained(skill_dir: str, rel: str) -> str | None:
    """Resolve ``rel`` under ``skill_dir``, returning the absolute path only if
    it stays inside ``skill_dir`` (defends against ``../`` escapes)."""
    root = os.path.realpath(skill_dir)
    resolved = os.path.realpath(os.path.join(root, rel))
    if resolved == root or resolved.startswith(root + os.sep):
        return resolved
    return None


def substitute_skill_vars(body: str, skill_dir: str, session_id: str = "") -> str:
    """Replace the ``${CLAUDE_SKILL_DIR}`` and ``${CLAUDE_SESSION_ID}``
    placeholders in a skill body."""
    return body.replace(_SKILL_DIR_VAR, skill_dir).replace(_SESSION_ID_VAR, session_id or "")


def rewrite_body(body: str, skill_dir: str) -> str:
    """Fallback: rewrite bare relative ``scripts/`` / ``references/`` /
    ``assets/`` references to absolute paths under ``skill_dir``. A reference
    that would escape the directory is left untouched."""

    def repl(m: re.Match) -> str:
        rel = m.group(1)
        abs_path = _contained(skill_dir, rel)
        return abs_path if abs_path else rel

    return _REL_REF_RE.sub(repl, body)


def resolve_body(body: str, skill_dir: str, session_id: str = "") -> str:
    """Full body resolution for a folder skill: substitute placeholders, rewrite
    bare relative refs, and prepend a resolved base-directory preamble so the
    model knows where the bundled files live and how to run them."""
    resolved = rewrite_body(substitute_skill_vars(body, skill_dir, session_id), skill_dir)
    example = os.path.join(skill_dir, "scripts", "NAME.py")
    header = (
        f"SKILL DIRECTORY: {skill_dir}\n"
        "(Bundled files live under that directory. To run a bundled script, call "
        "terminal_run or run_python with its ABSOLUTE path, e.g. "
        f"`python3 {example}`.)\n\n"
    )
    return header + resolved
