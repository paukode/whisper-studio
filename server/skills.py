import json
import logging
import os

from fastapi import APIRouter

from server import folder_skills
from server.executors import EXECUTORS, emits_model_prompt
from server.infrastructure.paths import data_root
from server.mcp import mcp_manager

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/skills", tags=["skills"])
# WHISPER.md endpoints (Feature 10 + 18) ride their own router, mounted
# separately in server/main.py. Both routers' handlers live in skills_routes.
whisper_md_router = APIRouter(prefix="/api/whisper-md", tags=["whisper-md"])

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
SKILLS_DIR = os.path.join(BASE_DIR, "skills")
DATA_DIR = data_root()
SKILLS_CONFIG_PATH = os.path.join(DATA_DIR, "skills_config.json")

# Global state
SKILLS: dict = {}
TOOLS: list[dict] = []

# Transcript-driven skills whose primary input can overflow the context window
# and therefore may need map-reduce condensation (see server.summarize.mapreduce).
_TRANSCRIPT_SKILLS = {"meeting_notes", "summarize_transcript", "catch_up"}


def load_skills(skills_dir: str = None) -> dict:
    """Load skill definitions from markdown files with YAML frontmatter."""
    skills_path = skills_dir or SKILLS_DIR
    skills = {}
    if not os.path.isdir(skills_path):
        return skills
    # Collect .md files from top-level and subdirectories (e.g. private/)
    md_files = []
    for fname in os.listdir(skills_path):
        fpath = os.path.join(skills_path, fname)
        if fname.endswith(".md"):
            md_files.append((skills_path, fname))
        elif os.path.isdir(fpath):
            # A subdir that is itself a folder skill (contains SKILL.md) is
            # loaded whole below — don't scrape its references/*.md as separate
            # skills.
            if folder_skills.is_folder_skill(fpath):
                continue
            for sub in os.listdir(fpath):
                if sub.endswith(".md"):
                    md_files.append((fpath, sub))
    for parent_dir, fname in md_files:
        filepath = os.path.join(parent_dir, fname)
        try:
            with open(filepath) as f:
                content = f.read()
            if not content.startswith("---"):
                continue
            # A file that opens with `---` but never closes the frontmatter
            # block raises ValueError here. SKIP it (mirroring
            # folder_skills.parse_frontmatter) — one malformed skill must not
            # abort the whole loader, which runs in the FastAPI lifespan.
            end = content.index("---", 3)
            frontmatter_text = content[3:end].strip()
            body = content[end + 3 :].strip()
            # Simple YAML parsing for flat keys
            frontmatter = {}
            for line in frontmatter_text.split("\n"):
                if line.startswith("  "):
                    continue
                if ":" in line:
                    key, val = line.split(":", 1)
                    frontmatter[key.strip()] = val.strip()
            # Parse input_schema from frontmatter
            input_props = {}
            required_fields = []
            in_schema = False
            current_field = None
            for line in frontmatter_text.split("\n"):
                if line.startswith("input_schema:"):
                    in_schema = True
                    continue
                if in_schema:
                    stripped = line.rstrip()
                    if stripped and not stripped.startswith(" "):
                        in_schema = False
                        continue
                    if stripped.startswith("  ") and not stripped.startswith("    "):
                        current_field = stripped.strip().rstrip(":")
                        input_props[current_field] = {}
                    elif stripped.startswith("    ") and current_field and ":" in stripped:
                        k, v = stripped.strip().split(":", 1)
                        k, v = k.strip(), v.strip()
                        if k == "required" and v == "true":
                            required_fields.append(current_field)
                        elif k == "type":
                            input_props[current_field]["type"] = v
                        elif k == "description":
                            input_props[current_field]["description"] = v.strip("\"'")
            schema_props = {}
            for field, props in input_props.items():
                schema_props[field] = {
                    "type": props.get("type", "string"),
                    "description": props.get("description", field),
                }
            if "style_options" in frontmatter and "style" in schema_props:
                schema_props["style"]["enum"] = [
                    s.strip() for s in frontmatter["style_options"].split(",")
                ]
            name = frontmatter.get("name", fname.replace(".md", "").replace("-", "_"))
            # Feature 9: per-skill model override
            skill_model = frontmatter.get("model", "").strip() or None
            skills[name] = {
                "name": name,
                "description": frontmatter.get("description", ""),
                "triggers": [
                    t.strip() for t in frontmatter.get("triggers", "").split(",") if t.strip()
                ],
                "executor": frontmatter.get("executor", ""),
                "model": skill_model,
                "body": body,
                "input_schema": {
                    "type": "object",
                    "properties": schema_props,
                    "required": required_fields,
                },
            }
            log.info("Loaded skill: %s (%d triggers)", name, len(skills[name]["triggers"]))
        except Exception as e:
            log.warning("Skipping malformed skill file %s: %s", filepath, e)
            continue
    # Folder skills: a directory with SKILL.md + optional scripts/references/assets.
    for _fname, _fdef in folder_skills.discover_folder_skills(skills_path).items():
        skills[_fname] = _fdef
    return skills


def load_skills_config() -> dict:
    try:
        with open(SKILLS_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {"disabled": []}


def save_skills_config(config: dict):
    os.makedirs(os.path.dirname(SKILLS_CONFIG_PATH), exist_ok=True)
    with open(SKILLS_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def is_trusted(skill_name: str) -> bool:
    """Whether the user has marked a skill trusted (gates allowed-tools
    auto-approval for folder skills)."""
    return skill_name in set(load_skills_config().get("trusted", []))


def command_runs_trusted_skill(command: str) -> bool:
    """True if ``command`` runs a file inside a *trusted* folder skill's own
    directory (e.g. ``python3 /…/skills/agenthub/scripts/x.py`` when the
    agenthub skill is trusted). Used to auto-approve a trusted skill running its
    own bundled scripts, without opening a hole for arbitrary commands: the
    match is scoped to the skill's own directory, and validate_command still
    applies. Untrusted skills always fall through to the approval card."""
    if not command:
        return False
    trusted = set(load_skills_config().get("trusted", []))
    if not trusted:
        return False
    for name in trusted:
        skill = SKILLS.get(name)
        skill_dir = skill.get("skill_dir") if skill else None
        if skill_dir and (skill_dir.rstrip(os.sep) + os.sep) in command:
            return True
    return False


def rebuild_tools():
    """Rebuild TOOLS list from SKILLS, excluding disabled ones."""
    disabled = set(load_skills_config().get("disabled", []))
    TOOLS.clear()
    for _skill_name, _skill_def in SKILLS.items():
        if _skill_name in disabled:
            continue
        TOOLS.append(
            {
                "name": _skill_def["name"],
                "description": _skill_def["description"],
                "input_schema": _skill_def["input_schema"],
            }
        )


def init_skills():
    """Load skills and build tools on startup."""
    global SKILLS
    SKILLS = load_skills()
    rebuild_tools()


def get_whisper_md_context(ws_path: str | None) -> str:
    """Feature 10: Load WHISPER.md from the workspace root as additional system context."""
    if not ws_path:
        return ""
    whisper_md = os.path.join(ws_path, "WHISPER.md")
    if not os.path.isfile(whisper_md):
        return ""
    try:
        with open(whisper_md, errors="replace") as f:
            content = f.read().strip()
        if content:
            return f"\n\n[WHISPER.md — project-specific instructions]\n{content}"
    except Exception as e:
        # Surface why custom context isn't applying — users were
        # silently getting no project memory when WHISPER.md had a
        # permission issue or other read failure.
        log.warning("WHISPER.md read failed at %s: %s", whisper_md, e)
    return ""


def get_skill_model(skill_name: str) -> str | None:
    """Feature 9: Return the model override for a skill, if specified."""
    skill = SKILLS.get(skill_name)
    if skill:
        return skill.get("model")
    return None


def _tool_run_skill(skill_name: str, tool_input: dict) -> str:
    """Generic handler for prompt-based skills (no executor)."""
    skill = SKILLS[skill_name]
    input_lines = "\n".join(
        f"{k}: {v}" for k, v in tool_input.items() if v and k != "__session_id__"
    )
    body = skill["body"]
    # Folder skills: resolve ${CLAUDE_SKILL_DIR} / relative script refs to
    # absolute paths and prepend the skill's base directory so the model can
    # run bundled scripts via terminal_run / run_python.
    skill_dir = skill.get("skill_dir")
    if skill_dir:
        session_id = str(tool_input.get("__session_id__", "") or "")
        body = folder_skills.resolve_body(body, skill_dir, session_id)
    return f"USER REQUEST:\n{input_lines}\n\nSKILL INSTRUCTIONS:\n{body}"


def produces_model_prompt(tool_name: str) -> bool:
    """True when a tool's result is a model PROMPT — skill instructions (or a
    content executor's style hint / question) wrapped around the user's input —
    rather than computed data output.

    Such results MUST bypass the oversize-output budgeter (server.chat.budget):
    the instructions sit at the payload's tail (see ``_tool_run_skill`` and the
    content executors), so head-truncation silently discards them and leaves
    the model with a headless transcript it cannot act on. Ordinary data
    outputs (command results, file reads, web/AWS calls) are still budgeted.

    Fails safe — returns False for unknown/MCP tool names and before skills are
    loaded (empty ``SKILLS``), so the default remains "budget it".
    """
    skill = SKILLS.get(tool_name)
    if skill:
        executor_name = skill.get("executor")
        if not executor_name:
            # Prompt / folder skill: _tool_run_skill returns instructions+input.
            return True
        # Executor-backed skill: defer to the executor's own declaration.
        return emits_model_prompt(executor_name)
    # A bare executor invoked directly as a tool (no skill wrapper).
    return emits_model_prompt(tool_name)


def execute_tool(
    tool_name: str, tool_input: dict, transcript: str = "", current_attachments: dict | None = None
) -> str:
    if mcp_manager.is_mcp_tool(tool_name):
        return "[MCP tools must be called via execute_mcp_tool]"
    skill = SKILLS.get(tool_name)
    if skill:
        # Defensive: transcript-driven skills (meeting_notes,
        # summarize_transcript, catch_up) expect a chunk of text as
        # their primary argument — usually called ``notes``,
        # ``text``, or ``transcript``. The user message hint already
        # asks Claude to pass the transcript, but if the model still
        # arrives here with an empty/short value AND a transcript
        # is in scope, fall back to the transcript rather than
        # rendering a useless empty prompt.
        if transcript and transcript.strip():
            props = (skill.get("input_schema") or {}).get("properties") or {}
            for arg_name in ("notes", "text", "transcript"):
                if arg_name in props:
                    current_val = str(tool_input.get(arg_name, "") or "").strip()
                    # Threshold: if the model's value is shorter than
                    # the transcript by an order of magnitude, it
                    # almost certainly grabbed the user's prompt by
                    # mistake instead of the transcript.
                    if len(current_val) < min(40, len(transcript) // 10):
                        tool_input = {**tool_input, arg_name: transcript}
                    break
        # A huge literal notes/text arg (the user pasted a wall of text with no
        # live recording, so the transcript variable is empty and was not
        # condensed upstream in routes.py) also overflows the context. Condense
        # it the same way. The live-transcript path is already condensed before
        # it reaches here.
        if tool_name in _TRANSCRIPT_SKILLS:
            from server.summarize.mapreduce import maybe_condense_transcript, threshold

            limit = threshold()
            # Condense every present transcript-bearing arg that is oversized.
            # _tool_run_skill renders all of them, so a short earlier key must
            # not stop us from condensing a later oversized one.
            for arg_name in ("notes", "text", "transcript"):
                val = str(tool_input.get(arg_name) or "")
                if len(val) > limit:
                    tool_input = {**tool_input, arg_name: maybe_condense_transcript(val)}
        executor_name = skill.get("executor")
        if executor_name and executor_name in EXECUTORS:
            return EXECUTORS[executor_name](tool_input, transcript, current_attachments)
        return _tool_run_skill(tool_name, tool_input)
    if tool_name in EXECUTORS:
        return EXECUTORS[tool_name](tool_input, transcript, current_attachments)
    return f"Unknown tool: {tool_name}"


async def execute_mcp_tool(tool_name: str, tool_input: dict) -> str:
    return await mcp_manager.call_tool(tool_name, tool_input)


def _find_skill_file(name: str) -> str | None:
    # Search top-level and subdirectories
    search_dirs = [SKILLS_DIR]
    for entry in os.listdir(SKILLS_DIR):
        sub = os.path.join(SKILLS_DIR, entry)
        if os.path.isdir(sub):
            search_dirs.append(sub)
    for sdir in search_dirs:
        for fname in os.listdir(sdir):
            if not fname.endswith(".md"):
                continue
            filepath = os.path.join(sdir, fname)
            try:
                with open(filepath) as f:
                    content = f.read()
                if not content.startswith("---"):
                    continue
                # Skip a file with unterminated frontmatter rather than letting
                # its ValueError abort the whole lookup (see load_skills).
                end = content.index("---", 3)
                fm = content[3:end].strip()
                parsed_name = None
                for line in fm.split("\n"):
                    if line.startswith("name:"):
                        parsed_name = line.split(":", 1)[1].strip()
                        break
            except Exception as e:
                log.warning("Skipping malformed skill file %s: %s", filepath, e)
                continue
            if parsed_name == name or fname.replace(".md", "").replace("-", "_") == name:
                return os.path.relpath(filepath, SKILLS_DIR)
    return None


# HTTP handlers for both routers live in the sibling skills_routes module;
# importing it registers them via decorator side-effects. Imported here (after
# the routers, skill state, and loader helpers are defined) so
# ``from server.skills import router, whisper_md_router`` yields wired routers.
from server import skills_routes  # noqa: E402,F401
