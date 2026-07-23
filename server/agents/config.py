"""
Agent configuration — type definitions and tool filtering.

Each agent type has a preset configuration controlling model selection,
tool access, turn limits, and isolation behavior.
"""

from dataclasses import dataclass, replace

from server.executors import EXECUTOR_META
from server.memory.prompts import (
    CONSOLIDATION_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    SESSION_SUMMARY_PROMPT,
)

# Tools that modify state — blocked for read-only agents.
#
# SECURITY INVARIANT (do not weaken): a read_only agent must never reach a
# tool that can write, execute, or otherwise mutate state. This matters
# because agents AUTO-APPROVE the `[WS_APPROVAL]` gate (see
# server/agents/runtime.py) — there is no human in the loop, so any
# write/execute-capable tool left in the pool runs unattended.
#
# This denylist is the guaranteed floor. `filter_tools_for_agent` ALSO drops
# any tool the executor registry marks `read_only=False` (the registry
# backstop below), so newly added *native* executors are gated automatically.
# Entries here therefore fall into three buckets:
#   1. Native writes registered `read_only=False` (also caught by the backstop;
#      listed here so the guarantee holds even before executor modules are
#      imported, e.g. aws_cli, run_python, terminal_run, ws_write_file, git_*).
#   2. Tools the registry cannot see — handler-/skill-backed tools that never
#      go through `@register_executor`, so the backstop will not catch them and
#      they MUST be listed here (config_set, task_*, cron_*, notebook_edit,
#      notify_user; also any future MCP write tool).
#   3. Tools registered `read_only=True` that must still be gated. `aws_boto3`
#      self-limits to read-shaped AWS calls, so the registry calls it
#      read_only, but a read_only sub-agent must not touch AWS at all — the
#      denylist overrides the registry for it.
# When adding a new write/execute tool, add it here. The backstop is a safety
# net, not a substitute: only native @register_executor(read_only=False) tools
# are covered by it, and only once their module is imported.
WRITE_TOOLS = frozenset(
    {
        "ws_write_file",
        "ws_create_file",
        "ws_edit_file",
        "ws_delete_file",
        "ws_run_command",
        "ws_create_worktree",
        "ws_merge_worktree",
        "config_set",
        "task_create",
        "task_update",
        "task_stop",
        "cron_create",
        "cron_delete",
        "notebook_edit",
        "git_add_commit",
        "git_push",
        "git_create_branch",
        "git_checkout",
        "git_merge",
        "git_stash",
        "git_push_pr",
        "git_delete_branch",
        "git_clone",
        "notify_user",
        "memory_write",
        "memory_delete",
        # Execution tools. aws_cli/run_python/terminal_run are registered
        # read_only=False (so the backstop also gates them, but listing them
        # keeps the guarantee if the registry isn't populated yet) and emit the
        # auto-approved [WS_APPROVAL] gate. aws_boto3 is registered
        # read_only=True because it self-limits to read-shaped AWS calls, so the
        # backstop does NOT gate it — a read_only agent must not touch AWS at
        # all, hence the explicit override here.
        "aws_cli",
        "aws_boto3",
        "run_python",
        "terminal_run",
    }
)

# Tools available to coordinators only
COORDINATOR_TOOLS = frozenset(
    {
        "spawn_agent",
        "send_message",
        "list_agents",
        "complete_coordination",
    }
)

# Memory-agent tool pools. Extraction and consolidation both mutate the store
# (write/merge/delete files), so they get the read+write set. The session
# summariser only PRODUCES text that its caller writes to the session-memory
# file, so it must stay read-only — no memory_write/memory_delete — or it would
# rewrite the two-tier store unattended (agents auto-approve the write gate).
MEMORY_RW_TOOLS = frozenset(
    {
        "memory_read",
        "memory_write",
        "memory_list",
        "memory_delete",
        "ws_read_file",
        "ws_grep",
        "ws_glob",
    }
)
MEMORY_RO_TOOLS = frozenset(
    {
        "memory_read",
        "memory_list",
        "ws_read_file",
        "ws_grep",
        "ws_glob",
    }
)

# Shared working method appended to every user-facing agent type's system
# prompt. Two goals: structured step-by-step work, and an explicit
# self-assessment BEFORE the final answer (the self-check surfaces in the
# team card's live log as a text event, so the user sees it happen).
AGENT_METHOD = (
    "\n\nMethod: first restate the task in one line and list the steps you "
    "will take. Then work step by step, using tools to gather evidence for "
    "every claim. Before returning, run a brief self-check: does the output "
    "fully answer the task, is each claim backed by evidence (file paths, "
    "line numbers, command output), and what gaps or uncertainty remain. "
    "State the self-check result in one or two lines, then give your final "
    "answer."
)


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for an agent type.

    Attributes:
        agent_type: Type identifier (general, explore, plan, verify, coordinator).
        model: Model key override (haiku, sonnet, opus, opus4.7). None = inherit parent.
        max_turns: Maximum tool-use rounds before forced stop (a runaway backstop,
            not the normal exit — pair it with deadline_seconds).
        deadline_seconds: Wall-clock budget for the whole run. None = no time limit.
            This is the primary "don't loop forever" brake: it stops a stuck agent
            regardless of how few or many turns it has taken.
        max_tokens: Max tokens per Bedrock response.
        read_only: If True, write tools are excluded from the tool pool.
        isolation: Isolation mode — "none" or "worktree".
        allowed_tools: If set, ONLY these tools are available (whitelist).
        denied_tools: If set, these tools are excluded (blacklist).
        system_prompt: Override system prompt. None = default subagent prompt.
    """

    agent_type: str = "general"
    model: str | None = None
    max_turns: int = 30
    deadline_seconds: float | None = None
    max_tokens: int = 16384
    read_only: bool = False
    isolation: str = "none"
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] | None = None
    system_prompt: str | None = None


AGENT_TYPES: dict[str, AgentConfig] = {
    "general": AgentConfig(
        agent_type="general",
        model=None,  # inherit the session-selected model (no hardcoded per-type model)
        # High turn backstop + a wall-clock deadline as the real brake: workflow
        # (ultracode) agents run as `general`, and a 30-turn cap was cutting them
        # off mid-task (surfacing as null structured output). Tune per-deployment
        # via config.json `agent_limits` (see get_agent_config).
        max_turns=120,
        deadline_seconds=900,
        max_tokens=16384,
    ),
    "explore": AgentConfig(
        agent_type="explore",
        model=None,  # inherit the session-selected model (no hardcoded per-type model)
        max_turns=30,
        deadline_seconds=600,
        max_tokens=8192,
        read_only=True,
        system_prompt=(
            "You are a fast exploration agent. Your job is to quickly search and read "
            "code to answer questions. Use ws_read_file, ws_grep, ws_glob, ws_list_directory, "
            "and git tools to find information. Be concise and direct in your findings. "
            "Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen."
        )
        + AGENT_METHOD,
    ),
    "plan": AgentConfig(
        agent_type="plan",
        model=None,  # inherit the session-selected model (no hardcoded per-type model)
        max_turns=40,
        deadline_seconds=600,
        max_tokens=16384,
        read_only=True,
        system_prompt=(
            "You are a planning agent. Analyze the codebase, understand the architecture, "
            "and produce a step-by-step implementation plan. Read files to understand the "
            "current state, identify dependencies, and outline changes needed. "
            "Return a structured plan with files to modify and specific changes. "
            "Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen."
        )
        + AGENT_METHOD,
    ),
    "verify": AgentConfig(
        agent_type="verify",
        model=None,  # inherit the session-selected model (no hardcoded per-type model)
        max_turns=30,
        deadline_seconds=600,
        max_tokens=8192,
        system_prompt=(
            "You are a verification agent. Check that the implementation is correct by "
            "reading code, running tests, and validating behavior. "
            "Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen. "
            "End your response with exactly one of:\n"
            "VERDICT: PASS, everything looks correct\n"
            "VERDICT: FAIL, issues found (list them)\n"
            "VERDICT: PARTIAL, some issues (list what passed and what failed)"
        )
        + AGENT_METHOD,
    ),
    "memory_extractor": AgentConfig(
        agent_type="memory_extractor",
        model=None,  # inherit the session-selected model (no hardcoded per-type model)
        max_turns=5,
        max_tokens=4096,
        allowed_tools=MEMORY_RW_TOOLS,
        # Single source of truth in server/memory/prompts.py (previously this
        # held a drifting inline copy of the same instructions).
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
    ),
    # Dream consolidation: reorganizes the EXISTING store (merge/update/prune),
    # so it keeps the write set but runs under a consolidation-specific prompt
    # instead of the extraction prompt (whose "what to save / what NOT to save"
    # rules conflict with a reorganize-only task). The scoped, phased plan is
    # passed as the task by server/memory/dream.py.
    "memory_consolidator": AgentConfig(
        agent_type="memory_consolidator",
        model=None,  # inherit the session-selected model (no hardcoded per-type model)
        max_turns=8,
        max_tokens=4096,
        allowed_tools=MEMORY_RW_TOOLS,
        system_prompt=CONSOLIDATION_SYSTEM_PROMPT,
    ),
    # Session summariser: distils one conversation into a fixed-section markdown
    # file. The caller writes that file from the agent's returned text, so the
    # agent itself is READ-ONLY (no memory_write/memory_delete) and runs under
    # the summary prompt, not the extraction prompt.
    "session_summarizer": AgentConfig(
        agent_type="session_summarizer",
        model=None,  # inherit the session-selected model (no hardcoded per-type model)
        max_turns=5,
        max_tokens=4096,
        allowed_tools=MEMORY_RO_TOOLS,
        system_prompt=SESSION_SUMMARY_PROMPT,
    ),
    "coordinator": AgentConfig(
        agent_type="coordinator",
        model=None,  # inherit the session-selected model (no hardcoded per-type model)
        max_turns=100,
        deadline_seconds=1800,
        max_tokens=16384,
        allowed_tools=COORDINATOR_TOOLS,
        system_prompt=(
            "You are a coordinator agent. You orchestrate other agents to complete "
            "complex tasks. You do NOT touch files directly, instead spawn specialized "
            "agents for each piece of work, monitor their progress via messages, and "
            "synthesize results. Available tools: spawn_agent, send_message, list_agents, "
            "complete_coordination. Use spawn_agent to create workers, send_message to "
            "communicate, and complete_coordination when all work is done. "
            "Do not use em dashes or en dashes; prefer commas, parentheses, a colon, or a short spaced hyphen."
        )
        + AGENT_METHOD,
    ),
}


def _agent_limit_overrides() -> dict:
    """Read the optional ``agent_limits`` block from config.json.

    Shape (every key optional)::

        "agent_limits": {
          "default":  {"max_turns": 120, "deadline_seconds": 900},
          "general":  {"max_turns": 200, "deadline_seconds": 1200},
          "explore":  {"deadline_seconds": null}   // null disables the time limit
        }

    Lets turn/time budgets be tuned per deployment without editing code.
    """
    try:
        from server.infrastructure.config import load_config

        ov = load_config().get("agent_limits")
        return ov if isinstance(ov, dict) else {}
    except Exception:
        return {}


def get_agent_config(agent_type: str) -> AgentConfig:
    """Resolve an agent config, applying config.json ``agent_limits`` overrides.

    Starts from the built-in ``AGENT_TYPES`` preset, then overlays ``default``
    followed by the type-specific block so a type entry wins. Only ``max_turns``
    and ``deadline_seconds`` are overridable; any unset key keeps the preset.
    """
    base = AGENT_TYPES.get(agent_type, AGENT_TYPES["general"])
    ov = _agent_limit_overrides()
    merged = {**(ov.get("default") or {}), **(ov.get(agent_type) or {})}
    if not merged:
        return base

    changes: dict = {}
    mt = merged.get("max_turns")
    if isinstance(mt, int) and mt > 0:
        changes["max_turns"] = mt
    if "deadline_seconds" in merged:
        ds = merged["deadline_seconds"]
        if ds is None:
            changes["deadline_seconds"] = None  # explicit: no time limit
        elif isinstance(ds, (int, float)) and ds > 0:
            changes["deadline_seconds"] = float(ds)
    return replace(base, **changes) if changes else base


def _is_write_tool(name: str) -> bool:
    """True if a tool must be kept away from a read_only agent.

    Two layers, both fail toward denial:
      1. Explicit denylist (WRITE_TOOLS) — always wins. Covers skill-/handler-/
         MCP-backed tools the executor registry never sees, plus tools that are
         registered read_only but must still be gated (e.g. aws_boto3).
      2. Registry backstop — any tool a native executor registered with
         `read_only=False` is a write, even if nobody remembered to add it to
         WRITE_TOOLS. This keeps future native executors gated by default.

    Tools that are neither denylisted nor known to the registry (e.g. an MCP
    read tool) are treated as reads and kept — the denylist is the place to add
    any such tool that actually writes.
    """
    if name in WRITE_TOOLS:
        return True
    meta = EXECUTOR_META.get(name)
    if meta is not None and not meta.get("read_only", False):
        return True
    return False


def filter_tools_for_agent(all_tools: list[dict], config: AgentConfig) -> list[dict]:
    """Filter a tool pool based on agent configuration.

    Applies in order:
    1. allowed_tools whitelist (if set, only these tools pass)
    2. denied_tools blacklist
    3. read_only filter (removes any write/execute-capable tool — see
       _is_write_tool for the denylist + registry-backstop logic)
    """
    tools = list(all_tools)

    if config.allowed_tools is not None:
        tools = [t for t in tools if t["name"] in config.allowed_tools]
        return tools

    if config.denied_tools:
        tools = [t for t in tools if t["name"] not in config.denied_tools]

    if config.read_only:
        tools = [t for t in tools if not _is_write_tool(t["name"])]

    return tools
