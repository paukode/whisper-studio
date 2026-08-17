"""
Custom and ephemeral agent type definitions.

Two ways an agent "type" can exist besides the hardcoded ``AGENT_TYPES`` table
in server/agents/config.py:

  - CUSTOM types: persistent presets stored as ``.whisper/agents/<name>.md``
    files (YAML frontmatter + a markdown body, the Jekyll/Hugo-style
    convention Claude Code itself uses for ``.claude/agents/*.md``).
    Project-scoped (``<workspace>/.whisper/agents/``, alongside the existing
    ``.whisper/settings.json`` project config — see
    server/infrastructure/config.py:_load_project_config) and user-scoped
    (``<config_dir()>/.whisper/agents/``, alongside the USER config layer —
    see server/infrastructure/config.py's ``config_dir()``/USER_CONFIG_PATH).
    A name collision resolves PROJECT-over-USER, mirroring how
    server/infrastructure/config.py layers the PROJECT config layer over the
    USER layer for regular settings.

  - EPHEMERAL types: built in-memory at ``spawn_agent`` call time from an
    inline definition the calling model supplies directly in the tool call
    (server/agent_tools/spawn.py), never written to disk unless later
    promoted via the ``promote_agent_type`` tool
    (server/agent_tools/promote.py).

SECURITY INVARIANT (do not weaken): every AgentConfig built here — from a
file or an inline definition — goes through the exact same WRITE_TOOLS /
registry-backstop filtering that built-in AGENT_TYPES entries do
(server/agents/config.py:filter_tools_for_agent). ``build_agent_config``
below additionally sanitizes a read_only config's own allowed_tools
whitelist against that same backstop at CONSTRUCTION time — belt and
suspenders around a tool-writable, LLM- or file-authored input, unlike the
trusted, hardcoded AGENT_TYPES presets. See filter_tools_for_agent's own
docstring for the matching runtime-side half of this fix.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass

from server.agents.config import AgentConfig, _is_write_tool

log = logging.getLogger("whisper-studio")

AGENTS_SUBDIR = os.path.join(".whisper", "agents")

_DEFAULT_MAX_TURNS = 30
_DEFAULT_DEADLINE_SECONDS = 600
_DEFAULT_MAX_TOKENS = 8192

# A name becomes a bare `<name>.md` filename component when promoted — never
# accept anything with path separators or `..` for it.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ── value coercion helpers ───────────────────────────────────────────────────


def _as_list(value) -> list[str] | None:
    """Accept a YAML list (PyYAML) or a comma-separated string (flat-parser
    fallback, matching server/folder_skills.py's own ``_as_list``)."""
    if value is None:
        return None
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = [v.strip() for v in str(value).split(",") if v.strip()]
    return items or None


def _as_bool_or_none(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None


def _as_int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def safe_type_name(raw: str) -> str | None:
    """A name safe to use as a bare ``<name>.md`` filename component.

    Rejects anything with path separators, ``..``, or characters outside
    ``[A-Za-z0-9_-]`` rather than silently mangling it — a custom/ephemeral
    agent type name can originate from an LLM tool call, and this name
    becomes a filesystem path component when promoted
    (server/agent_tools/promote.py), so it must never be trusted blindly for
    path construction.
    """
    name = (raw or "").strip()
    return name if _NAME_RE.match(name) else None


# ── shared config builder (custom files AND ephemeral defs both use this) ───


def build_agent_config(
    name: str,
    *,
    tools=None,
    read_only=None,
    model: str | None = None,
    max_turns=None,
    system_prompt: str | None = None,
    default_read_only: bool = False,
) -> AgentConfig:
    """Build an AgentConfig from custom/ephemeral fields.

    ``default_read_only`` is the fallback used when the definition supplies
    NEITHER ``tools`` NOR ``read_only`` explicitly. A persistent custom file
    (human-authored, reviewed by whoever wrote it under .whisper/agents/)
    defaults to False, matching the built-in ``general`` preset. An ephemeral
    type an LLM invents mid-turn, with no human ever reviewing the
    definition before it runs, should pass ``default_read_only=True`` — the
    more conservative choice, since this is exactly the "no human in the
    loop" situation the WRITE_TOOLS invariant exists for.

    Resolution order for read_only: an explicit ``read_only`` always wins;
    otherwise a non-empty ``tools`` list implies "just these tools, writes
    included if listed" (read_only=False, matching the built-in
    memory_extractor pattern of an explicit whitelist with no read_only
    flag); only when NEITHER is given does ``default_read_only`` apply.

    SECURITY: when the resolved config is read_only, its own allowed_tools
    whitelist (if any) is intersected against the WRITE_TOOLS/registry
    backstop right here, so a definition that sets both ``read_only: true``
    and a ``tools:`` list containing a write tool cannot use the whitelist
    path in filter_tools_for_agent to bypass its own read_only flag.
    """
    tool_list = _as_list(tools)
    ro = _as_bool_or_none(read_only)
    if ro is not None:
        is_read_only = ro
    elif tool_list:
        is_read_only = False
    else:
        is_read_only = default_read_only
    allowed = frozenset(tool_list) if tool_list else None

    if is_read_only and allowed:
        safe = frozenset(t for t in allowed if not _is_write_tool(t))
        if safe != allowed:
            log.warning(
                "custom/ephemeral agent type %r requested read_only=True with "
                "write tool(s) in its allowed_tools whitelist; stripping them: %s",
                name,
                sorted(allowed - safe),
            )
        allowed = safe or None

    prompt = (system_prompt or "").strip() or None
    mt = _as_int_or_none(max_turns)

    return AgentConfig(
        agent_type=name,
        max_turns=mt if mt and mt > 0 else _DEFAULT_MAX_TURNS,
        deadline_seconds=_DEFAULT_DEADLINE_SECONDS,
        max_tokens=_DEFAULT_MAX_TOKENS,
        read_only=is_read_only,
        allowed_tools=allowed,
        system_prompt=prompt,
        model=(str(model).strip() or None) if model else None,
    )


# ── Part 1: persistent custom types from .whisper/agents/*.md ───────────────


def _project_agents_dir(workspace_path: str | None) -> str | None:
    if not workspace_path:
        return None
    return os.path.join(workspace_path, AGENTS_SUBDIR)


def _user_agents_dir() -> str:
    from server.infrastructure.paths import repo_root, user_root

    # Packaged install: plain <~/.whisper>/agents — nesting ".whisper/agents"
    # inside a directory already named .whisper read as an accident. A dev
    # checkout keeps the historical repo/.whisper/agents (gitignored).
    root = user_root()
    if root != repo_root():
        return os.path.join(root, "agents")
    return os.path.join(root, AGENTS_SUBDIR)


def _parse_agent_md(path: str) -> AgentConfig | None:
    from server.folder_skills import parse_frontmatter

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        log.warning("custom agent type unreadable at %s: %s", path, e)
        return None

    fm, body = parse_frontmatter(content)
    if not isinstance(fm, dict):
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    name = str(fm.get("name") or stem).strip() or stem
    return build_agent_config(
        name,
        tools=fm.get("tools"),
        read_only=fm.get("read_only"),
        model=fm.get("model"),
        max_turns=fm.get("max_turns"),
        system_prompt=body,
        default_read_only=False,
    )


def _load_agents_dir(directory: str | None) -> dict[str, AgentConfig]:
    out: dict[str, AgentConfig] = {}
    if not directory or not os.path.isdir(directory):
        return out
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return out
    for entry in entries:
        if not entry.endswith(".md"):
            continue
        cfg = _parse_agent_md(os.path.join(directory, entry))
        if cfg is not None:
            out[cfg.agent_type] = cfg
    return out


def load_custom_agent_types(workspace_path: str | None = None) -> dict[str, AgentConfig]:
    """Scan USER then PROJECT ``.whisper/agents/*.md``, PROJECT winning a name
    collision — the same precedence server/infrastructure/config.py uses for
    the PROJECT config layer over the USER layer (see its module docstring:
    "Config layers (lowest to highest priority): ... USER ... PROJECT").

    ``workspace_path`` defaults to the currently connected workspace
    (server.workspace.get_workspace_path()) when not given explicitly.
    """
    if workspace_path is None:
        try:
            from server.workspace import get_workspace_path

            workspace_path = get_workspace_path()
        except Exception:
            workspace_path = None

    types = _load_agents_dir(_user_agents_dir())
    types.update(_load_agents_dir(_project_agents_dir(workspace_path)))
    return types


# ── Part 2: ephemeral, LLM-created types ─────────────────────────────────────


@dataclass(frozen=True)
class EphemeralAgentDef:
    """The raw fields an inline spawn_agent ``agent_definition`` supplied, kept
    verbatim so promote_agent_type can RE-DERIVE (never blindly reuse) the
    AgentConfig at promote time."""

    name: str
    description: str
    tools: tuple[str, ...] | None
    read_only: bool | None
    model: str | None
    max_turns: int | None
    system_prompt: str | None


_lock = threading.Lock()

# session_id -> {name: EphemeralAgentDef} — lets promote_agent_type look an
# ephemeral definition up later by the name the model gave it. Deliberately
# simple: in-process only, no persistence, no cross-session visibility.
_SESSION_EPHEMERAL: dict[str, dict[str, EphemeralAgentDef]] = {}

# resolution_key -> (registered_at, AgentConfig). Lets get_agent_config()
# resolve an ephemeral type by the same string-name mechanism the UNMODIFIED
# detached-agent runtime (server/tasks/agents.py) already uses for named
# types, so a detached ephemeral spawn gets the identical
# read-only-unless-worktree-isolated guardrail with NO parallel code path.
_RUNTIME_EPHEMERAL: dict[str, tuple[float, AgentConfig]] = {}
_RUNTIME_EPHEMERAL_MAX = 200


def _prune_runtime_ephemeral_locked() -> None:
    if len(_RUNTIME_EPHEMERAL) <= _RUNTIME_EPHEMERAL_MAX:
        return
    oldest_first = sorted(_RUNTIME_EPHEMERAL.items(), key=lambda kv: kv[1][0])
    for key, _ in oldest_first[: len(_RUNTIME_EPHEMERAL) - _RUNTIME_EPHEMERAL_MAX]:
        _RUNTIME_EPHEMERAL.pop(key, None)


def register_ephemeral_type(session_id: str, definition: dict) -> tuple[str, AgentConfig, dict]:
    """Build an ephemeral AgentConfig from an inline spawn_agent definition.

    Returns ``(resolution_key, config, meta)``: resolution_key is a unique
    string usable anywhere an ``agent_type`` name is accepted (including the
    unmodified detached-agent path), config is the built AgentConfig (already
    run through the same WRITE_TOOLS sanitizing as any custom/built-in type),
    and meta is the small dict a tool result surfaces to the calling model
    (``{"name", "description", "read_only"}``).
    """
    raw_name = str(definition.get("name") or "").strip() or "ephemeral_agent"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", raw_name)[:64] or "ephemeral_agent"
    description = str(definition.get("description") or "").strip()
    tools = _as_list(definition.get("tools"))
    read_only = definition.get("read_only")
    model = definition.get("model")
    max_turns = definition.get("max_turns")
    system_prompt = definition.get("system_prompt")

    config = build_agent_config(
        name,
        tools=tools,
        read_only=read_only,
        model=model,
        max_turns=max_turns,
        system_prompt=system_prompt,
        # No human ever reviews an ephemeral definition before it runs — if
        # the model didn't ask for tools or state read_only explicitly, fail
        # CLOSED (read-only), not open.
        default_read_only=True,
    )

    resolution_key = f"{name}~{uuid.uuid4().hex[:8]}"
    with _lock:
        _RUNTIME_EPHEMERAL[resolution_key] = (time.monotonic(), config)
        _prune_runtime_ephemeral_locked()
        _SESSION_EPHEMERAL.setdefault(session_id, {})[name] = EphemeralAgentDef(
            name=name,
            description=description,
            tools=tuple(tools) if tools else None,
            read_only=_as_bool_or_none(read_only),
            model=(str(model).strip() or None) if model else None,
            max_turns=_as_int_or_none(max_turns),
            system_prompt=system_prompt,
        )

    meta = {"name": name, "description": description, "read_only": config.read_only}
    return resolution_key, config, meta


def get_ephemeral_type(resolution_key: str) -> AgentConfig | None:
    """Resolve a runtime ephemeral key registered by register_ephemeral_type.
    Consulted by get_agent_config() BEFORE custom files and AGENT_TYPES."""
    with _lock:
        entry = _RUNTIME_EPHEMERAL.get(resolution_key)
    return entry[1] if entry else None


def get_session_ephemeral_def(session_id: str, name: str) -> EphemeralAgentDef | None:
    with _lock:
        return _SESSION_EPHEMERAL.get(session_id, {}).get(name)


# ── Part 3: promote an ephemeral def to a persistent custom-type file ───────


def _agent_md_text(config: AgentConfig, description: str) -> str:
    """Reverse-serialize an AgentConfig back into a ``.whisper/agents/<name>.md``
    file (YAML frontmatter + markdown body) — the exact format
    load_custom_agent_types()/_parse_agent_md reads back, so a promoted type
    round-trips through the loader unchanged."""
    lines = ["---", f"name: {config.agent_type}"]
    if description:
        lines.append(f"description: {description}")
    if config.allowed_tools:
        lines.append("tools:")
        for t in sorted(config.allowed_tools):
            lines.append(f"  - {t}")
    lines.append(f"read_only: {'true' if config.read_only else 'false'}")
    if config.model:
        lines.append(f"model: {config.model}")
    lines.append(f"max_turns: {config.max_turns}")
    lines.append("---")
    body = (config.system_prompt or "").strip()
    text = "\n".join(lines) + "\n"
    if body:
        text += "\n" + body + "\n"
    return text


def promote_ephemeral_type(
    session_id: str,
    name: str,
    *,
    workspace_path: str | None = None,
    scope: str = "project",
) -> tuple[AgentConfig, str] | None:
    """Re-validate a session's ephemeral definition and write it to disk.

    Returns ``(config, path)`` on success, or None if ``name`` has no
    recorded ephemeral definition for this session. Never trusts the
    previously-built AgentConfig: rebuilds it from the RAW recorded fields
    through build_agent_config() again, so the same WRITE_TOOLS/read_only
    sanitizing runs at promote time too, not just at spawn time.
    """
    stored = get_session_ephemeral_def(session_id, name)
    if stored is None:
        return None

    config = build_agent_config(
        stored.name,
        tools=list(stored.tools) if stored.tools else None,
        read_only=stored.read_only,
        model=stored.model,
        max_turns=stored.max_turns,
        system_prompt=stored.system_prompt,
        default_read_only=True,
    )

    safe_name = safe_type_name(config.agent_type)
    if safe_name is None:
        raise ValueError(f"unsafe agent type name: {config.agent_type!r}")

    if scope == "user":
        directory = _user_agents_dir()
    else:
        directory = _project_agents_dir(workspace_path)
        if directory is None:
            raise ValueError("no workspace connected; cannot write a project-scoped agent type")

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{safe_name}.md")
    text = _agent_md_text(config, stored.description)
    tmp = os.path.join(directory, f".{safe_name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return config, path
