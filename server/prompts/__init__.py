"""System prompt builder — structured, layered construction.

Assembles the system prompt from a section registry with priority ordering.
Each section is a named, prioritized block that can be independently enabled,
disabled, or overridden.

Architecture (inspired by Claude Code's 6-layer system):
  1. Identity (base persona) — always included, highest priority
  2. Mode modifiers (brief, plan) — conditional on session config
  3. Workspace context (code rules, ws instructions) — conditional on workspace
  4. Dynamic context (git status, WHISPER.md, transcript) — per-request
  5. Tool guidance (task tracking, MCP) — always included
  6. Extensions (custom injections) — lowest priority

The static/dynamic boundary exists at layer 3/4: layers 1-3 are stable within
a session (good for prompt caching), while layers 4-6 change per request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum

from server.git.prompts import build_git_instructions_prompt, build_git_status_prompt
from server.prompts.base import BASE
from server.prompts.modes import BRIEF_PREFIX, PLAN_MODE
from server.prompts.rules import rules_block as _user_rules_block
from server.prompts.workspace import (
    CODE_OUTPUT_RULE_GENERIC,
    CODE_OUTPUT_RULE_WORKSPACE,
    workspace_prompt,
)

log = logging.getLogger("whisper-studio")

# Appended when the Ultracode effort mode is active (Opus 4.8 / Fable 5). It is
# more than an effort value: it directs the model to orchestrate parallel
# subagents on substantive work, mirroring Claude Code's dynamic workflows.
ULTRACODE_DIRECTIVE = (
    "\n\n## Ultracode mode\n"
    "Ultracode mode is active — optimise for the most thorough, correct result; "
    "token cost is not a constraint. For substantive, multi-part, or long-horizon "
    "tasks, decompose the work and orchestrate parallel subagents with the "
    "`spawn_agent` and `team_create` tools rather than doing everything sequentially "
    "in one context. Write every agent task as a self-contained brief (the agent "
    "sees none of this conversation): objective restated from the user's request, "
    "scope and inputs, constraints, and the expected output format with acceptance "
    "criteria. Fan out independent workstreams, then verify and reconcile their "
    "results before answering. For trivial or conversational turns, just answer "
    "directly — do not spawn agents for simple work."
)


class PromptLayer(IntEnum):
    """Priority layers for prompt sections. Lower number = higher priority = earlier in prompt."""

    IDENTITY = 10
    MODE = 20
    WORKSPACE = 30
    DYNAMIC = 40
    TOOL_GUIDANCE = 50
    EXTENSION = 60


@dataclass
class PromptSection:
    """A named, prioritized block of system prompt text."""

    name: str
    layer: PromptLayer
    priority: int = 0  # Within-layer ordering (lower = earlier)
    content: str = ""
    builder: Callable[..., str] | None = None  # Dynamic content builder
    enabled: bool = True
    prepend: bool = False  # If True, content goes before everything at this layer

    def resolve(self, **kwargs) -> str:
        """Resolve section content — static string or dynamic builder."""
        if not self.enabled:
            return ""
        if self.builder:
            try:
                return self.builder(**kwargs)
            except Exception as e:
                log.warning("Prompt section '%s' builder failed: %s", self.name, e)
                return ""
        return self.content


class PromptRegistry:
    """Registry of prompt sections that assembles them in priority order."""

    def __init__(self):
        self._sections: dict[str, PromptSection] = {}

    def register(self, section: PromptSection) -> None:
        """Register or replace a prompt section."""
        self._sections[section.name] = section

    def unregister(self, name: str) -> None:
        """Remove a section by name."""
        self._sections.pop(name, None)

    def get(self, name: str) -> PromptSection | None:
        return self._sections.get(name)

    def enable(self, name: str) -> None:
        if name in self._sections:
            self._sections[name].enabled = True

    def disable(self, name: str) -> None:
        if name in self._sections:
            self._sections[name].enabled = False

    def get_sections(self) -> list[PromptSection]:
        """Return all sections sorted by layer then priority."""
        return sorted(
            self._sections.values(),
            key=lambda s: (s.layer, s.priority),
        )

    def get_section_names(self) -> list[str]:
        """Return names of all registered sections in order."""
        return [s.name for s in self.get_sections()]


# ── Global registry ──────────────────────────────────────────────────
_registry = PromptRegistry()


def get_registry() -> PromptRegistry:
    """Access the global prompt section registry."""
    return _registry


# ── Section builders ─────────────────────────────────────────────────


def _build_workspace_section(ws_path: str | None = None, **_) -> str:
    if not ws_path:
        return ""
    return workspace_prompt(ws_path)


def _build_no_workspace_section(ws_path: str | None = None, **_) -> str:
    """Injected when NO workspace is connected. The write executors
    (ws_create_file / ws_write_file / ws_edit_file / ws_delete_file)
    emit a [WS_WORKSPACE_PROMPT] payload that renders a folder picker
    to the user. After the user picks, a continuation turn arrives
    telling the LLM to re-issue the same tool call, which then hits
    the normal [WS_APPROVAL] flow. So the LLM should NOT pre-prompt
    the user with ask_user_question or guess a path via ws_open_folder —
    the system handles workspace selection automatically."""
    if ws_path:
        return ""
    return (
        "\n\nNO CODE WORKSPACE: No workspace folder is connected. "
        "If the user asks to create, edit, or delete files: call the "
        "write tool directly (ws_create_file / ws_write_file / "
        "ws_edit_file / ws_delete_file) with the intended relative "
        "path. The system will automatically show the user a folder "
        "picker before the write happens, then resume this turn with "
        "the workspace connected, you will then re-issue the same "
        "tool call and it will go through the normal approval flow. "
        "Do NOT call ask_user_question to ask where to save. Do NOT "
        "call ws_open_folder with a guessed path. Only call "
        "ws_open_folder if the user explicitly asks to open a specific "
        "folder. If the user only wants a conversation or a one-off "
        "code snippet, respond normally with a fenced code block, do "
        "not call any write tool."
    )


def _build_git_section(ws_path: str | None = None, **_) -> str:
    if not ws_path:
        return ""
    from server.infrastructure.feature_flags import is_enabled

    if not is_enabled("git_context"):
        return ""
    git_context = build_git_status_prompt(ws_path)
    if git_context:
        return "\n" + git_context + "\n" + build_git_instructions_prompt()
    return ""


def _build_task_tracking_section(session_id: str = "default", **_) -> str:
    guidance = (
        "\n\nTASK TRACKING: For complex multi-step requests, use task_create to create tasks, "
        "task_update to mark them in_progress/completed, and task_list to review progress. "
        "When you begin a fresh multi-step plan after an earlier one is already finished, "
        "task_stop the old completed tasks first so the task list reflects only the current plan. "
        f"Always pass session_id='{session_id}' to task tools."
    )
    # Inject the live task list (with IDs) so a resumed or continued turn keeps
    # working the SAME plan instead of re-deriving it and creating duplicates.
    # The task tracker is stored out-of-band from the chat transcript, so after
    # a page refresh the model would otherwise come back blind to the tasks it
    # already created. Reading the list here also self-heals stray in_progress
    # state (get_session_tasks enforces a single active task).
    try:
        from server.tasks_tracker import get_session_tasks

        tasks = get_session_tasks(session_id)
    except Exception:
        tasks = []
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

# User-editable global output rules (root PROMPT_RULES.md). Empty file → no rule.
# Sits at the identity layer so it's part of the stable, cached prefix.
_registry.register(
    PromptSection(
        name="user_rules",
        layer=PromptLayer.IDENTITY,
        priority=5,
        builder=lambda **_: _user_rules_block(),
    )
)

_registry.register(
    PromptSection(
        name="brief_mode",
        layer=PromptLayer.MODE,
        priority=0,
        content=BRIEF_PREFIX,
        enabled=False,  # Enabled dynamically
        prepend=True,
    )
)

_registry.register(
    PromptSection(
        name="plan_mode",
        layer=PromptLayer.MODE,
        priority=10,
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
        builder=_build_git_section,
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

_registry.register(
    PromptSection(
        name="terminal_run_guidance",
        layer=PromptLayer.TOOL_GUIDANCE,
        priority=5,
        content="""
# Running shell commands (terminal_run)

You have a `terminal_run` tool that executes shell commands. Use it for:
"install package X", "check the version of Y", "run the tests", "what's in
that file", "see if Z is available", etc.

Two modes:
  • `mode='sandbox'` (default): runs in a hidden ephemeral shell. Use this
    for probes and checks: the user doesn't see it scroll, no rc-file noise
    pollutes the output, and the session is destroyed afterwards. Side
    effects on the host filesystem still persist (it's the same machine).
  • `mode='visible'`: writes into the user's currently-open terminal so
    they can watch the command stream in real time. Use ONLY when the user
    explicitly says "in the terminal", "so I can see", "where I can watch",
    or similar. Requires an open terminal session in the UI.

Rules:
  • Non-interactive only. Commands waiting on stdin (vim, less, top, ssh
    password prompts, sudo without -n) are refused before execution.
  • Default timeout 30s, max 300s. If a long-running install is expected,
    pass `timeout` explicitly.
  • Prefer `terminal_run` over asking the user to run something. Asking is
    friction; `terminal_run` is one approval click.
  • If a workspace is connected, the working directory defaults to it.
    Pass `cwd` only when you specifically need a different location.
""",
    )
)


def _build_preview_guidance_section(ws_path: str | None = None, **_) -> str:
    """Steer the model to run dev servers through the preview tools so they
    render in the right-side Live pane, never a browser tab. Emitted only when
    the preview tools are actually in the catalog (flag on + Chromium installed)
    — otherwise the model would be told to call tools it doesn't have."""
    try:
        from server.infrastructure.feature_flags import is_enabled

        if not is_enabled("preview_tools"):
            return ""
        from server.preview.capability import preview_capability_ok

        if not preview_capability_ok():
            return ""
    except Exception:
        return ""
    return """
# Running a dev server / previewing an app (preview_start)

When the user wants to SEE a running app, site, or dev server (anything that
serves a page — Vite, Next.js, `npm run dev`, a static server, an HTTP API with
a UI), start it with `preview_start`, NOT terminal_run, and NEVER by printing a
localhost URL for the user to open in their browser. `preview_start` runs the
server; then `preview_navigate` loads the page into the right-side Live pane.
terminal_run can't do this (sandbox mode kills the server on timeout, and either
way it stays invisible to the preview pane).

  • ALWAYS pass `session_name` (it names the preview session — it is required
    and is never replaced by the command args). For the command, prefer a named
    config in `.whisper/launch.json` (same shape as Claude Code's
    `.claude/launch.json`), in which case `session_name` alone suffices; if the
    project has no launch.json, add one, or pass `runtimeExecutable`/
    `runtimeArgs` alongside `session_name` (e.g. session_name="web",
    runtimeExecutable="npm", runtimeArgs=["run","dev"]).
  • After preview_start, call preview_navigate to load the page (nothing is
    visible until you do), then preview_screenshot / preview_snapshot /
    preview_console_logs to inspect it.
  • Reuse a running session instead of starting a duplicate; stop it with
    preview_stop when done.
  • Only fall back to terminal_run for one-shot builds or servers the user
    explicitly does not want previewed. Do not tell the user to open a
    localhost link themselves — the Live pane is the preview surface.
"""


_registry.register(
    PromptSection(
        name="preview_guidance",
        layer=PromptLayer.TOOL_GUIDANCE,
        priority=6,
        builder=_build_preview_guidance_section,
    )
)

_registry.register(
    PromptSection(
        name="whisper_md",
        layer=PromptLayer.DYNAMIC,
        priority=10,
        content="",  # Set dynamically
    )
)


# ── Main build function ──────────────────────────────────────────────


def _resolve_sections(
    *,
    ws_path: str | None,
    session_id: str,
    brief_mode: bool = False,
    plan_mode: bool = False,
    whisper_md_context: str = "",
    memory_context: str = "",
    session_memory_context: str = "",
    ultracode: bool = False,
    deferred_tool_index: str = "",
) -> tuple[str, str]:
    """Resolve the section registry into a ``(static, dynamic)`` pair.

    - ``static``  = layers <= WORKSPACE (30): identity, mode, workspace rules.
      Session-stable, so safe to prompt-cache.
    - ``dynamic`` = layers >= DYNAMIC (40): memory, git status, session memory,
      tool guidance, ultracode directive. Changes per request.

    Invariant: ``static + dynamic`` is byte-identical to the single-string prompt
    the legacy builder produced. So callers that don't cache are unaffected, and
    the cached static prefix never drifts from the uncached string.

    The legacy builder concatenates ALL ``prepend`` sections before ALL ``main``
    sections, across layers. Byte-equality therefore holds only if no dynamic
    section uses ``prepend`` (otherwise the split would reorder it). If a dynamic
    prepend ever appears, we fall back to putting everything in the dynamic block
    (caching disabled for that build, correctness preserved) and log it.
    """
    registry = get_registry()

    # Configure mode sections (identical to the legacy builder).
    brief_section = registry.get("brief_mode")
    if brief_section:
        brief_section.enabled = brief_mode

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
    }

    static_prepend: list[str] = []
    static_main: list[str] = []
    dyn_prepend: list[str] = []
    dyn_main: list[str] = []
    for section in registry.get_sections():
        if not section.enabled:
            continue
        text = section.resolve(**kwargs)
        if not text:
            continue
        is_static = int(section.layer) <= int(PromptLayer.WORKSPACE)
        if section.prepend:
            (static_prepend if is_static else dyn_prepend).append(text)
        else:
            (static_main if is_static else dyn_main).append(text)

    if dyn_prepend:
        # A dynamic-layer prepend would be reordered by the split — unsafe.
        # Put the whole prompt in the (uncached) dynamic block instead.
        log.warning("System-prompt split disabled: a dynamic-layer section uses prepend.")
        full = (
            "".join(static_prepend)
            + "".join(dyn_prepend)
            + "".join(static_main)
            + "".join(dyn_main)
        )
        if ws_path:
            full = full.replace(CODE_OUTPUT_RULE_GENERIC, CODE_OUTPUT_RULE_WORKSPACE)
        if ultracode:
            from server.prompts.ultracode import build_ultracode_directive

            full += build_ultracode_directive()
        return "", full

    static_block = "".join(static_prepend) + "".join(static_main)
    # The generic->workspace code-rule swap targets an IDENTITY/WORKSPACE section
    # (layer <= 30), so it lives entirely within the static block.
    if ws_path:
        static_block = static_block.replace(CODE_OUTPUT_RULE_GENERIC, CODE_OUTPUT_RULE_WORKSPACE)

    dynamic_block = "".join(dyn_main)
    if ultracode:
        from server.prompts.ultracode import build_ultracode_directive

        dynamic_block += build_ultracode_directive()

    return static_block, dynamic_block


def build_system_prompt(
    *,
    ws_path: str | None,
    session_id: str,
    brief_mode: bool = False,
    plan_mode: bool = False,
    whisper_md_context: str = "",
    memory_context: str = "",
    session_memory_context: str = "",
    ultracode: bool = False,
    deferred_tool_index: str = "",
) -> str:
    """Build the complete system prompt as a single string (unchanged contract).

    This is the main entry point for callers that do not cache (local models,
    compaction, etc.). It delegates to :func:`_resolve_sections` and joins.
    """
    static_block, dynamic_block = _resolve_sections(
        ws_path=ws_path,
        session_id=session_id,
        brief_mode=brief_mode,
        plan_mode=plan_mode,
        whisper_md_context=whisper_md_context,
        memory_context=memory_context,
        session_memory_context=session_memory_context,
        ultracode=ultracode,
        deferred_tool_index=deferred_tool_index,
    )
    return static_block + dynamic_block


def build_system_prompt_split(
    *,
    ws_path: str | None,
    session_id: str,
    brief_mode: bool = False,
    plan_mode: bool = False,
    whisper_md_context: str = "",
    memory_context: str = "",
    session_memory_context: str = "",
    ultracode: bool = False,
    deferred_tool_index: str = "",
) -> tuple[str, str]:
    """Like :func:`build_system_prompt` but returns ``(static, dynamic)`` so the
    cloud path can prompt-cache the static prefix. By construction,
    ``static + dynamic == build_system_prompt(<same args>)`` exactly.
    """
    return _resolve_sections(
        ws_path=ws_path,
        session_id=session_id,
        brief_mode=brief_mode,
        plan_mode=plan_mode,
        whisper_md_context=whisper_md_context,
        memory_context=memory_context,
        session_memory_context=session_memory_context,
        ultracode=ultracode,
        deferred_tool_index=deferred_tool_index,
    )
