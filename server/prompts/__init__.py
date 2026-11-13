"""System prompt builder: structured, layered construction.

Assembles the system prompt from a section registry with priority ordering.
Each section is a named, prioritized block, enabled per request.

Layers:
  1. Identity (base persona, code-output rule, user rules): always included
  2. Mode (brief, the permission-mode line): conditional on session config
  3. Workspace (workspace and git instructions, project instructions,
     learning guidance, rules capture, reply style, visuals, deferred tool index)
  4. Dynamic (plan mode, memory, session memory, git status): per request
  5. Tool guidance (task tracking and the live task list): per request

The static/dynamic boundary is between layers 3 and 4: layers 1-3 are stable
within a session (prompt-cached), while layers 4-5 change per request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

from server.git.prompts import build_git_instructions_prompt, build_git_status_prompt
from server.prompts.base import BASE
from server.prompts.modes import BYPASS_MODE, GUARDED_MODE, PLAN_MODE
from server.prompts.reply_style import REPLY_STYLE
from server.prompts.rules import rules_block as _user_rules_block
from server.prompts.visuals import VISUAL_CHOICE
from server.prompts.workspace import (
    CODE_OUTPUT_RULE_GENERIC,
    CODE_OUTPUT_RULE_WORKSPACE,
    workspace_prompt,
)

__all__ = ["build_system_prompt", "build_system_prompt_split", "get_registry"]


class PromptLayer(IntEnum):
    """Priority layers for prompt sections. Lower number = higher priority = earlier in prompt."""

    IDENTITY = 10
    MODE = 20
    WORKSPACE = 30
    DYNAMIC = 40
    TOOL_GUIDANCE = 50


@dataclass
class PromptSection:
    """A named, prioritized block of system prompt text."""

    name: str
    layer: PromptLayer
    priority: int = 0  # Within-layer ordering (lower = earlier)
    content: str = ""
    builder: Callable[..., str] | None = None  # Dynamic content builder
    enabled: bool = True

    def resolve(self, **kwargs) -> str:
        """Resolve section content — static string or dynamic builder."""
        if not self.enabled:
            return ""
        if self.builder:
            return self.builder(**kwargs)
        return self.content


class PromptRegistry:
    """Registry of prompt sections that assembles them in priority order."""

    def __init__(self):
        self._sections: dict[str, PromptSection] = {}

    def register(self, section: PromptSection) -> None:
        """Register or replace a prompt section."""
        self._sections[section.name] = section

    def get(self, name: str) -> PromptSection | None:
        return self._sections.get(name)

    def get_sections(self) -> list[PromptSection]:
        """Return all sections sorted by layer then priority."""
        return sorted(
            self._sections.values(),
            key=lambda s: (s.layer, s.priority),
        )


# ── Global registry ──────────────────────────────────────────────────
_registry = PromptRegistry()


def get_registry() -> PromptRegistry:
    """Access the global prompt section registry."""
    return _registry


# ── Section builders ─────────────────────────────────────────────────


def _build_code_output_rule(ws_path: str | None = None, **_) -> str:
    """Pick the code-output rule that matches the current surface.

    With a workspace the model edits files via ws_* tools; without one the reply
    itself is the deliverable, so it must carry complete fenced code. Emitting
    both at once tells the model to do two contradictory things, so this is a
    branch, never a concatenation.
    """
    return CODE_OUTPUT_RULE_WORKSPACE if ws_path else CODE_OUTPUT_RULE_GENERIC


def _build_workspace_section(
    ws_path: str | None = None, session_id: str = "default", permission_mode: str = "", **_
) -> str:
    if not ws_path:
        return ""
    return workspace_prompt(ws_path, session_id, permission_mode)


def _build_no_workspace_section(ws_path: str | None = None, **_) -> str:
    """Injected when NO workspace is connected.

    The harness resolves the missing workspace itself: a write tool called
    without one returns a [WS_WORKSPACE_PROMPT] payload, the user picks a folder,
    and the turn resumes so the same call runs against it. That is not
    inferable from any tool schema, so it stays in the prompt — but the
    per-tool half of it now lives in the write tools' own descriptions.
    """
    if ws_path:
        return ""
    return (
        "\n\nNO CODE WORKSPACE: No folder is connected yet, but the write tools still work — "
        "call one with the relative path you intend and the user is shown a folder picker, "
        "after which this turn resumes and you re-issue the same call. So do not ask where to "
        "save, and do not guess a path with ws_open_folder (use that only when the user names a "
        "folder to open). For a conversation or a throwaway snippet, just answer; no write tool."
    )


# The permissions.json value for Bypass (server.security.permissions.MODE_BYPASS).
# Spelled out here so the prompt package does not import the security module.
_BYPASS = "bypassPermissions"
_PLAN = "plan"

# The last mode other than Plan that each session's prompt was built with. Plan
# is a mode of its own in permissions.json, but the plan-mode text rides the
# DYNAMIC layer so a toggle keeps the cached block. Reading Plan as "the mode
# you came from" for the static sections keeps that promise for Bypass too: a
# Bypass, Plan, Bypass flip leaves the static bytes alone.
_BASE_MODE: dict[str, str] = {}
_BASE_MODE_CAP = 512


def _static_permission_mode(session_id: str, permission_mode: str) -> str:
    """The mode the cached sections are built for (Plan reads as its base)."""
    if permission_mode != _PLAN:
        if permission_mode:
            _BASE_MODE.pop(session_id, None)
            _BASE_MODE[session_id] = permission_mode
            while len(_BASE_MODE) > _BASE_MODE_CAP:
                _BASE_MODE.pop(next(iter(_BASE_MODE)))
        return permission_mode
    return _BASE_MODE.get(session_id, permission_mode)


def _build_permission_mode_section(permission_mode: str = "", **_) -> str:
    """The line that says what the permission mode lets the model do.

    Bypass gets the "act without check-ins" line; every other mode gets the
    literal-delete-paths rule, which is the same text for all of them, so a
    flip between Default, Plan and Accept edits leaves the cached block alone.
    """
    return BYPASS_MODE if permission_mode == _BYPASS else GUARDED_MODE


def _git_context_on(ws_path: str | None) -> bool:
    """Whether the git prompt sections apply: a workspace, the flag, a repo.

    Checked by both git sections so they appear and disappear together. Uses
    ``find_git_root`` rather than the status output, so the static instructions
    do not have to run git commands to learn whether they are wanted.
    """
    if not ws_path:
        return False
    from server.infrastructure.feature_flags import is_enabled

    if not is_enabled("git_context"):
        return False
    from server.git.core import find_git_root

    return bool(find_git_root(ws_path))


def _build_git_instructions_section(ws_path: str | None = None, **_) -> str:
    """Fixed git workflow rules. Static, so it sits in the cached prefix."""
    if not _git_context_on(ws_path):
        return ""
    return "\n" + build_git_instructions_prompt()


def _build_git_status_section(ws_path: str | None = None, **_) -> str:
    """Live branch / dirty state / recent commits. Genuinely per-request."""
    if not _git_context_on(ws_path):
        return ""
    status = build_git_status_prompt(ws_path)
    return "\n" + status if status else ""


def _build_task_tracking_section(session_id: str = "default", **_) -> str:
    guidance = (
        "\n\nTASK TRACKING: For complex multi-step requests, use task_create to create tasks, "
        "task_update to mark them in_progress/completed, and task_list to review progress. "
        "When you begin a fresh multi-step plan after an earlier one is already finished, "
        "task_stop the old completed tasks first so the task list reflects only the current plan."
    )
    # Inject the live task list (with IDs) so a resumed or continued turn keeps
    # working the SAME plan instead of re-deriving it and creating duplicates.
    # The task tracker is stored out-of-band from the chat transcript, so after
    # a page refresh the model would otherwise come back blind to the tasks it
    # already created. Reading the list here also self-heals stray in_progress
    # state (get_session_tasks enforces a single active task).
    from server.tasks_tracker import get_session_tasks

    tasks = get_session_tasks(session_id)
    if tasks:
        rendered = "\n".join(
            f"  - [{t.get('status', 'pending')}] {t.get('id')}: {t.get('subject', '')}"
            for t in tasks
        )
        guidance += (
            "\n\nEXISTING TASKS for this session (already created — do NOT recreate them):\n"
            f"{rendered}\n"
            "Continue this plan: call task_update with the id shown above to advance a task "
            "(mark the next one in_progress, then completed when it is done). Only call "
            "task_create for genuinely new work that is not already listed, and keep exactly "
            "one task in_progress at a time."
        )
    return guidance


# ── Register built-in sections ───────────────────────────────────────

_registry.register(
    PromptSection(
        name="identity",
        layer=PromptLayer.IDENTITY,
        priority=0,
        content=BASE,
    )
)

_registry.register(
    PromptSection(
        name="code_output_rule",
        layer=PromptLayer.IDENTITY,
        priority=3,
        builder=_build_code_output_rule,
    )
)

# The user's global instructions and rules (~/.whisper/WHISPER.md and
# ~/.whisper/rules/global/, server/prompts/rules.py). Sits at the identity
# layer so it's part of the stable, cached prefix.
_registry.register(
    PromptSection(
        name="user_rules",
        layer=PromptLayer.IDENTITY,
        priority=5,
        builder=lambda **_: _user_rules_block(),
    )
)


# The permission-mode line rides the cached MODE layer, unlike plan mode below:
# the user changes the mode rarely, and a session in Bypass must see the Bypass
# line on every round without paying for it as uncached input.
_registry.register(
    PromptSection(
        name="permission_mode",
        layer=PromptLayer.MODE,
        priority=10,
        builder=_build_permission_mode_section,
    )
)

# Plan mode rides the DYNAMIC (uncached) layer on purpose, unlike its MODE
# siblings: it toggles mid-session, and a toggle that rewrites the static
# block invalidates the cached prefix twice per planning session (on + off).
# Parking the ~100 fixed tokens in the uncached tail costs pennies while
# active and keeps the static block byte-stable across the flip.
_registry.register(
    PromptSection(
        name="plan_mode",
        layer=PromptLayer.DYNAMIC,
        priority=5,
        content=PLAN_MODE,
        enabled=False,  # Enabled dynamically
    )
)

_registry.register(
    PromptSection(
        name="workspace",
        layer=PromptLayer.WORKSPACE,
        priority=0,
        builder=_build_workspace_section,
    )
)

_registry.register(
    PromptSection(
        name="no_workspace",
        layer=PromptLayer.WORKSPACE,
        priority=0,
        builder=_build_no_workspace_section,
    )
)

# Static git workflow rules. Deliberately on the WORKSPACE (cached) layer while
# its sibling "git_context" — the live status — stays DYNAMIC. Both are gated by
# _git_context_on, so they still appear and disappear as a pair.
_registry.register(
    PromptSection(
        name="git_instructions",
        layer=PromptLayer.WORKSPACE,
        priority=20,
        builder=_build_git_instructions_section,
    )
)


def _build_learning_guidance(**_) -> str:
    """When to reach for session_search and skill_manage. Flag-gated, and the
    flags are session-stable, so this sits in the cached STATIC block."""
    from server.infrastructure.feature_flags import is_enabled

    parts: list[str] = []
    if is_enabled("session_search"):
        parts.append(
            "Past sessions are searchable with session_search. Before asking the user to "
            "repeat something from an earlier session, or something from this session that "
            "compaction summarized away, look it up there first."
        )
    if is_enabled("skill_self_improvement"):
        parts.append(
            "Skills are your procedural memory. When the user asks you to remember a way of "
            "working, or corrects a multi-step procedure you will need again, encode it with "
            "skill_manage (view the relevant skill first, patch it if one covers the task, "
            "create a class-level skill only when nothing fits). Memory holds facts about the "
            "user; skills hold how to do a class of task."
        )
    return ("\n\n" + " ".join(parts)) if parts else ""


_registry.register(
    PromptSection(
        name="learning_guidance",
        layer=PromptLayer.WORKSPACE,
        priority=30,
        builder=_build_learning_guidance,
    )
)


# Where a rule the user states goes. The files themselves are loaded above
# (user_rules for the global layer, whisper_md for the project layer); this
# is the routing the model follows when the user asks it to remember one.
RULES_CAPTURE = (
    "\n\nRULES AND INSTRUCTIONS: The user's instructions above come from their own files under "
    "~/.whisper, and rules_write is how you change them. A behaviour the user asks you to keep is "
    "a rule for that tool, condensed to one line, not a memory; the tool's description says which "
    "target fits. Name the file you wrote to in one line of your reply."
)

_registry.register(
    PromptSection(
        name="rules_capture",
        layer=PromptLayer.WORKSPACE,
        priority=35,
        content=RULES_CAPTURE,
    )
)

# How a chat turn replies: quiet while it works, one final reply with the
# outcome first, formatted to read at a glance, and a short recap table of the
# files written (server/prompts/reply_style.py). Fixed text on the cached
# layer. It names plan mode itself instead of being switched off by it, so it
# adds no rewrite of the static block when the mode flips.
_registry.register(
    PromptSection(
        name="reply_style",
        layer=PromptLayer.WORKSPACE,
        priority=36,
        content=REPLY_STYLE,
    )
)

# When a reply should carry a diagram or chart the user did not ask for, and
# that visuals take the app theme on their own (server/prompts/visuals.py).
# Fixed text on the cached layer, like the reply style rule above.
_registry.register(
    PromptSection(
        name="visual_choice",
        layer=PromptLayer.WORKSPACE,
        priority=37,
        content=VISUAL_CHOICE,
    )
)


def _build_deferred_tool_index(deferred_tool_index: str | None = None, **_) -> str:
    """Progressive tool disclosure: the compact list of not-loaded tools.

    Lives at the END of the WORKSPACE layer so it stays inside the
    prompt-cached STATIC block — deterministic (sorted upstream) and only
    changes when the tool catalog changes, which busts the tools cache
    prefix anyway, so it adds zero extra cache invalidations.
    """
    return deferred_tool_index or ""


_registry.register(
    PromptSection(
        name="deferred_tool_index",
        layer=PromptLayer.WORKSPACE,
        priority=90,  # render last within the static block
        builder=_build_deferred_tool_index,
    )
)

_registry.register(
    PromptSection(
        name="session_memory_context",
        layer=PromptLayer.DYNAMIC,
        priority=-10,  # Before memory_context
        content="",
    )
)

_registry.register(
    PromptSection(
        name="memory_context",
        layer=PromptLayer.DYNAMIC,
        priority=-5,  # Before git_context
        content="",
    )
)

_registry.register(
    PromptSection(
        name="git_context",
        layer=PromptLayer.DYNAMIC,
        priority=0,
        builder=_build_git_status_section,
    )
)

_registry.register(
    PromptSection(
        name="task_tracking",
        layer=PromptLayer.TOOL_GUIDANCE,
        priority=0,
        builder=_build_task_tracking_section,
    )
)

# The connected project's instructions and rules (~/.whisper/rules/<slug>/,
# server/whisper_md.py). Content-injected per request, but it only changes
# when the user edits a file, so it belongs in the cached static block.
_registry.register(
    PromptSection(
        name="whisper_md",
        layer=PromptLayer.WORKSPACE,
        priority=10,
        content="",  # Set per request from the connected workspace
    )
)


# ── Main build function ──────────────────────────────────────────────


def _resolve_sections(
    *,
    ws_path: str | None,
    session_id: str,
    plan_mode: bool = False,
    whisper_md_context: str = "",
    memory_context: str = "",
    session_memory_context: str = "",
    ultracode: bool = False,
    deferred_tool_index: str = "",
    permission_mode: str = "",
) -> tuple[str, str]:
    """Resolve the section registry into a ``(static, dynamic)`` pair.

    - ``static``  = layers <= WORKSPACE (30): identity, mode, workspace rules,
      git workflow instructions. Session-stable, so safe to prompt-cache.
    - ``dynamic`` = layers >= DYNAMIC (40): memory, git status, session memory,
      tool guidance, ultracode directive. Changes per request.

    The split is what makes the cheap half cheap, so a section belongs in
    ``static`` unless its text genuinely varies per request. Static text parked
    on a DYNAMIC layer is re-billed as uncached input on every round of every
    turn, which is how ~1.5K tokens of fixed git instructions came to ride along
    behind 120 tokens of real git status.

    Invariant: ``static + dynamic`` is byte-identical to the single-string
    prompt (build_system_prompt), so the cached static prefix never drifts from
    the uncached string.
    """
    registry = get_registry()

    # Configure the per-request sections.
    plan_section = registry.get("plan_mode")
    if plan_section:
        plan_section.enabled = plan_mode and bool(ws_path)

    mem_section = registry.get("memory_context")
    if mem_section:
        mem_section.content = memory_context or ""
        mem_section.enabled = bool(memory_context)

    sess_mem_section = registry.get("session_memory_context")
    if sess_mem_section:
        sess_mem_section.content = session_memory_context or ""
        sess_mem_section.enabled = bool(session_memory_context)

    whisper_md_section = registry.get("whisper_md")
    if whisper_md_section:
        whisper_md_section.content = whisper_md_context or ""
        whisper_md_section.enabled = bool(whisper_md_context)

    kwargs = {
        "ws_path": ws_path,
        "session_id": session_id,
        "deferred_tool_index": deferred_tool_index,
        "permission_mode": _static_permission_mode(session_id, permission_mode),
    }

    static_main: list[str] = []
    dyn_main: list[str] = []
    for section in registry.get_sections():
        if not section.enabled:
            continue
        is_static = int(section.layer) <= int(PromptLayer.WORKSPACE)
        text = section.resolve(**kwargs)
        if not text:
            continue
        (static_main if is_static else dyn_main).append(text)

    static_block = "".join(static_main)
    dynamic_block = "".join(dyn_main)
    if ultracode:
        from server.prompts.ultracode import build_ultracode_directive

        dynamic_block += build_ultracode_directive()

    return static_block, dynamic_block


def build_system_prompt(
    *,
    ws_path: str | None,
    session_id: str,
    plan_mode: bool = False,
    whisper_md_context: str = "",
    memory_context: str = "",
    session_memory_context: str = "",
    ultracode: bool = False,
    deferred_tool_index: str = "",
    permission_mode: str = "",
) -> str:
    """Build the complete system prompt as a single string (unchanged contract).

    This is the main entry point for callers that do not cache (local models,
    compaction, etc.). It delegates to :func:`_resolve_sections` and joins.
    """
    static_block, dynamic_block = _resolve_sections(
        ws_path=ws_path,
        session_id=session_id,
        plan_mode=plan_mode,
        whisper_md_context=whisper_md_context,
        memory_context=memory_context,
        session_memory_context=session_memory_context,
        ultracode=ultracode,
        deferred_tool_index=deferred_tool_index,
        permission_mode=permission_mode,
    )
    return static_block + dynamic_block


def build_system_prompt_split(
    *,
    ws_path: str | None,
    session_id: str,
    plan_mode: bool = False,
    whisper_md_context: str = "",
    memory_context: str = "",
    session_memory_context: str = "",
    ultracode: bool = False,
    deferred_tool_index: str = "",
    permission_mode: str = "",
) -> tuple[str, str]:
    """Like :func:`build_system_prompt` but returns ``(static, dynamic)`` so the
    cloud path can prompt-cache the static prefix. By construction,
    ``static + dynamic == build_system_prompt(<same args>)`` exactly.
    """
    return _resolve_sections(
        ws_path=ws_path,
        session_id=session_id,
        plan_mode=plan_mode,
        whisper_md_context=whisper_md_context,
        memory_context=memory_context,
        session_memory_context=session_memory_context,
        ultracode=ultracode,
        deferred_tool_index=deferred_tool_index,
        permission_mode=permission_mode,
    )
