"""A read_only agent must never keep a write/execute-capable tool.

Agents auto-approve the ``[WS_APPROVAL]`` gate (server/agents/runtime.py), so
there is no human in the loop. Any execution tool left in a read_only agent's
pool would run unattended and could mutate state. These tests pin the two
layers of `filter_tools_for_agent`'s read_only filter: the explicit
``WRITE_TOOLS`` denylist and the executor-registry backstop.
"""

from server.agents.config import (
    WRITE_TOOLS,
    AgentConfig,
    filter_tools_for_agent,
)


def _pool(*names: str) -> list[dict]:
    """Build a minimal tool pool (the filter only reads each tool's name)."""
    return [{"name": n} for n in names]


def _names(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools}


# Read tools that a read_only agent must always keep.
READ_TOOLS = ("ws_read_file", "grep_search", "web_fetch")
# Execution/write tools that must be stripped for a read_only agent. These are
# skill-backed (no @register_executor) and would otherwise auto-approve.
EXEC_TOOLS = ("aws_cli", "run_python", "aws_boto3")


def test_read_only_agent_drops_execution_tools_keeps_read_tools():
    pool = _pool(*READ_TOOLS, *EXEC_TOOLS)
    config = AgentConfig(agent_type="explore", read_only=True)

    result = _names(filter_tools_for_agent(pool, config))

    # Execution/write tools removed.
    for tool in EXEC_TOOLS:
        assert tool not in result, f"{tool} must be removed for a read_only agent"
    # Read tools survive.
    for tool in READ_TOOLS:
        assert tool in result, f"{tool} must remain for a read_only agent"


def test_execution_tools_are_in_the_denylist():
    # The skill-backed execution tools the registry cannot see must be pinned
    # in the explicit denylist (the registry backstop cannot catch them).
    for tool in ("aws_cli", "aws_boto3", "run_python", "terminal_run"):
        assert tool in WRITE_TOOLS, f"{tool} must be in WRITE_TOOLS"


def test_aws_boto3_gated_despite_registry_read_only():
    # aws_boto3 self-limits to read-shaped AWS calls, so the executor registry
    # marks it read_only=True. A read_only sub-agent must still not touch AWS,
    # so the denylist overrides the registry for it.
    import server.executors.code  # noqa: F401 — populate the registry
    from server.executors import EXECUTOR_META

    assert EXECUTOR_META.get("aws_boto3", {}).get("read_only") is True
    result = _names(
        filter_tools_for_agent(_pool("aws_boto3", "ws_read_file"), AgentConfig(read_only=True))
    )
    assert "aws_boto3" not in result
    assert "ws_read_file" in result


def test_registry_backstop_drops_native_write_not_in_denylist():
    # A native executor registered read_only=False must be dropped even when it
    # is NOT listed in WRITE_TOOLS — this is the future-proofing backstop.
    import server.executors.preview  # noqa: F401 — registers preview_* metadata

    assert "preview_click" not in WRITE_TOOLS  # not denylisted...
    pool = _pool("preview_click", "preview_list", "ws_read_file")
    result = _names(filter_tools_for_agent(pool, AgentConfig(read_only=True)))

    assert "preview_click" not in result, "registry read_only=False tool must be dropped"
    assert "preview_list" in result, "registry read_only=True tool must remain"
    assert "ws_read_file" in result


def test_non_read_only_agent_keeps_everything():
    pool = _pool(*READ_TOOLS, *EXEC_TOOLS, "ws_write_file")
    config = AgentConfig(agent_type="general", read_only=False)

    result = _names(filter_tools_for_agent(pool, config))

    assert result == _names(pool), "non-read_only agents must keep every tool"


def test_allowed_tools_whitelist_bypasses_read_only_filter():
    # The whitelist path returns early; it should not be affected by WRITE_TOOLS.
    pool = _pool("memory_read", "memory_write", "ws_read_file")
    config = AgentConfig(
        agent_type="memory_extractor",
        read_only=False,
        allowed_tools=frozenset({"memory_read", "memory_write"}),
    )

    result = _names(filter_tools_for_agent(pool, config))

    assert result == {"memory_read", "memory_write"}
